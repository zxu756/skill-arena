"""Unified headless harness driver routing real agents through agent-relay (`relay.sh`) and `agent-xray`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import tempfile
import time
import traceback
import uuid

from skill_arena.graders.deterministic import cap_grader_output, evaluate_sandbox, verify_test_runner_available
from skill_arena.models import (
    ContenderConfig,
    GraderVerdict,
    TaskSpec,
    TokenUsageLedger,
    TrajectoryStep,
    TrialResult,
)
from skill_arena.outcomes import classify_trial_failure, is_interrupted
from skill_arena.runners.xray_bridge import XRayMeteringBridge
from skill_arena.sandbox.skill_injector import SkillInjector, SkillMountReport
from skill_arena.sandbox.worktree_pool import BoundedCommandOutcome, SandboxHandle, WorktreeSandboxPool

SUPPORTED_RELAY_CLIS: frozenset[str] = frozenset({"claude", "codex", "agy", "vibe"})

_REAL_MODEL_OR_SKILL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:claude|sonnet|opus|haiku|anthropic|gpt|openai|o1|o3|o4|codex|gemini|deepseek|mistral|qwen|llama|grok|"
    r"opencode|vibe|antigravity|agy|hallmark|superpowers)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PATH_LIKE_IN_TEXT_RE = re.compile(
    r"(?:^|\s)(?:~/|/(?:Users|home|tmp|var|etc|opt|private)/|\.\.?/)[^\s]+|(?:\.agents|\.claude)/[^\s]*|\b[\w./-]+/SKILL\.md\b",
    re.IGNORECASE,
)
_RELAY_HEADER_RE = re.compile(
    r"^## (\S+?): (.*), (?:(\d+)s|done in-session by the conductor), (\d{4}-\d\d-\d\d \d\d:\d\d)$"
)
_RELAY_COST_RE = re.compile(r"\bcost \$([0-9]+(?:\.[0-9]+)?)")
_RELAY_TOKENS_RE = re.compile(r"\btokens:? ([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)")


@dataclass(slots=True)
class RelayStepRecord:
    """Parsed metadata from `<worktree>/.relay/LOG.md` and companion step logs."""

    step_id: str | None = None
    status: str | None = None
    elapsed_sec: int | None = None
    cost_usd: float | None = None
    tokens: int | None = None
    changed_files: list[str] = field(default_factory=list)
    final_message: str = ""
    log_md_found: bool = False
    raw_log_tail: str = ""


@dataclass(slots=True)
class AgentRunInfo:
    """What the driver learned about one agent run beyond token usage (for failure categories)."""

    wall_ms: int | None = None
    relay_wrote_log: bool = True
    final_message: str = ""
    agent_texts: tuple[str, ...] = ()
    changed_files: list[str] = field(default_factory=list)


def _real_account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def resolve_relay_script(explicit_path: str | Path | None = None) -> Path:
    """Resolve `agent-relay/scripts/relay.sh` without hardcoded host paths."""
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    env_override = os.environ.get("SKILL_ARENA_RELAY_SCRIPT", "").strip()
    if env_override:
        candidates.append(Path(env_override).expanduser())
    rel_relay = Path(".agents") / "skills" / "agent-relay" / "scripts" / "relay.sh"
    candidates.append(Path.home() / rel_relay)
    candidates.append(_real_account_home() / rel_relay)

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    raise FileNotFoundError(
        "Could not locate `agent-relay/scripts/relay.sh`. "
        "Pass `--relay-script <path>` or set `SKILL_ARENA_RELAY_SCRIPT`."
    )


_ALLOWED_SYSTEM_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TERM",
    "COLORTERM",
    "USER",
    "LOGNAME",
    "SHELL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "VIRTUAL_ENV",
)

_ALLOWED_RELAY_ENV_KEYS: tuple[str, ...] = (
    "RELAY_CODEX_BIN",
    "RELAY_CLAUDE_BIN",
    "RELAY_AGY_BIN",
    "RELAY_VIBE_BIN",
    "RELAY_CLAUDE_MODEL",
    "RELAY_AGY_MODEL",
    "RELAY_AGY_ALLOW",
    "RELAY_VIBE_MAX_PRICE",
    "RELAY_ALLOW_SECRETS",
    "RELAY_TIMEOUT",
    "RELAY_BUDGET_USD",
    "RELAY_SKIP_NESTED_CHECK",
    "RELAY_WORKER",
)

_FORBIDDEN_CHILD_ENV_KEYS: tuple[str, ...] = (
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "CLAUDECODE",
)

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DISALLOWED_PASS_ENV_KEYS: frozenset[str] = frozenset(
    {
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "VIBE_HOME",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "CLAUDECODE",
        "RELAY_WORKER",
    }
)


def normalize_pass_env_names(
    raw_items: list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Validate and deduplicate explicit `--pass-env` variable names (no wildcards, `=`, or sandbox overrides)."""
    if not raw_items:
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        parts = str(raw).split(",")
        for part in parts:
            name = part.strip()
            if not name or not _ENV_VAR_NAME_RE.match(name):
                raise ValueError(
                    f"Invalid `--pass-env` variable name {part!r}: pass exact environment variable identifiers "
                    "(e.g. `--pass-env OPENAI_API_KEY`), without `=` values or wildcards. "
                    f"/ 无效的 `--pass-env` 变量名 {part!r}：请填写精确的环境变量名（不支持 `=` 赋值或通配符）。"
                )
            if (
                name in _DISALLOWED_PASS_ENV_KEYS
                or name.startswith("CLAUDE_CODE_")
                or name.startswith("SKILL_ARENA_")
            ):
                raise ValueError(
                    f"`--pass-env` refuses sandbox/session-protected variable {name!r}. "
                    f"/ `--pass-env` 拒绝透传沙盒或会话受保护变量 {name!r}。"
                )
            if name not in seen:
                seen.add(name)
                cleaned.append(name)
    return tuple(cleaned)


def _build_allowlisted_trial_env(
    mount_env: dict[str, str],
    trial_tmp: Path,
    pass_env: list[str] | tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Build child agent environment strictly from system allowlist, explicit `pass_env`, and per-trial paths."""
    allowed: dict[str, str] = {}
    for key in (*_ALLOWED_SYSTEM_ENV_KEYS, *_ALLOWED_RELAY_ENV_KEYS):
        val = os.environ.get(key)
        if val is not None:
            allowed[key] = val
    for key in normalize_pass_env_names(pass_env):
        val = os.environ.get(key)
        if val is not None:
            allowed[key] = val
    allowed.update(mount_env)
    tmp_str = str(trial_tmp)
    allowed["TMPDIR"] = tmp_str
    allowed["TEMP"] = tmp_str
    allowed["TMP"] = tmp_str
    for forbidden in _FORBIDDEN_CHILD_ENV_KEYS:
        allowed.pop(forbidden, None)
    for key in [k for k in allowed if k.startswith("CLAUDE_CODE_")]:
        allowed.pop(key, None)
    return allowed


def _inject_host_vendor_bin_if_needed(base_cli: str, env: dict[str, str]) -> None:
    """Pin `RELAY_<VENDOR>_BIN` before changing child HOME so `relay.sh` finds installed CLIs."""
    env_key = f"RELAY_{base_cli.upper()}_BIN"
    if env.get(env_key) or os.environ.get(env_key):
        return
    search_path = env.get("PATH") or os.environ.get("PATH")
    found = shutil.which(base_cli, path=search_path)
    if found:
        env[env_key] = found
        return

    host_home = _real_account_home()
    fallbacks: list[Path] = []
    if base_cli == "codex":
        fallbacks.extend(
            [
                Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
                Path("/Applications/Codex.app/Contents/Resources/codex"),
            ]
        )
    elif base_cli == "agy":
        fallbacks.append(host_home / ".local" / "bin" / "agy")
    elif base_cli == "claude":
        fallbacks.extend(
            [
                host_home / ".local" / "bin" / "claude",
                host_home / ".claude" / "local" / "claude",
            ]
        )
        desktop_root = host_home / "Library" / "Application Support" / "Claude" / "claude-code"
        if desktop_root.is_dir():
            versioned = sorted(
                desktop_root.glob("*/*/claude.app/Contents/MacOS/claude"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                reverse=True,
            )
            fallbacks.extend(versioned)
    elif base_cli == "vibe":
        fallbacks.append(host_home / ".local" / "bin" / "vibe")

    for candidate in fallbacks:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            env[env_key] = str(candidate)
            return


def parse_relay_artifacts(workdir: Path) -> RelayStepRecord:
    """Parse `<workdir>/.relay/LOG.md` summing step costs across all steps in `LOG.md`."""
    relay_dir = workdir / ".relay"
    log_md = relay_dir / "LOG.md"
    rec = RelayStepRecord()
    if not log_md.is_file():
        return rec
    rec.log_md_found = True

    lines = log_md.read_text(encoding="utf-8", errors="replace").splitlines()
    step_records: list[RelayStepRecord] = []
    cur: RelayStepRecord | None = None
    in_changed = False
    for line in lines:
        m_hdr = _RELAY_HEADER_RE.match(line)
        if m_hdr:
            cur = RelayStepRecord(
                step_id=m_hdr.group(1),
                status=m_hdr.group(2),
                elapsed_sec=int(m_hdr.group(3)) if m_hdr.group(3) else None,
            )
            step_records.append(cur)
            in_changed = False
            continue
        if cur is None:
            continue
        if line.strip() == "- files changed:":
            in_changed = True
            continue
        if in_changed:
            if line.startswith("  - "):
                entry = line[4:].strip()
                if entry and entry != "(none)":
                    cur.changed_files.append(entry)
                continue
            in_changed = False
        if line.startswith("- "):
            m_cost = _RELAY_COST_RE.search(line)
            if m_cost and cur.cost_usd is None:
                cur.cost_usd = float(m_cost.group(1))
            m_tok = _RELAY_TOKENS_RE.search(line)
            if m_tok and cur.tokens is None:
                cur.tokens = int(m_tok.group(1).replace(",", ""))

    if not step_records:
        return rec

    last_step = step_records[-1]
    non_ok = next((s.status for s in step_records if s.status and s.status != "ok"), None)
    elapsed_vals = [int(s.elapsed_sec) for s in step_records if s.elapsed_sec is not None]
    log_md_costs = [float(s.cost_usd) for s in step_records if s.cost_usd is not None]
    log_md_tokens = [int(s.tokens) for s in step_records if s.tokens is not None]
    merged_changed: list[str] = []
    for s in step_records:
        for fn in s.changed_files:
            if fn not in merged_changed:
                merged_changed.append(fn)

    rec = RelayStepRecord(
        step_id=last_step.step_id,
        status=non_ok or last_step.status,
        elapsed_sec=sum(elapsed_vals) if elapsed_vals else None,
        cost_usd=round(sum(log_md_costs), 6) if log_md_costs else None,
        tokens=sum(log_md_tokens) if log_md_tokens else None,
        changed_files=merged_changed,
        log_md_found=True,
    )

    if rec.step_id:
        out_path = relay_dir / f"{rec.step_id}.out.md"
        if out_path.is_file():
            rec.final_message = out_path.read_text(encoding="utf-8", errors="replace").strip()

        vibe_export = relay_dir / f"{rec.step_id}.vibe" / "export.json"
        if vibe_export.is_file():
            try:
                v_data = json.loads(vibe_export.read_text(encoding="utf-8", errors="replace"))
                if rec.cost_usd is None and isinstance(v_data.get("cost_usd"), (int, float)):
                    rec.cost_usd = float(v_data["cost_usd"])
                v_usage = v_data.get("usage")
                if rec.tokens is None and isinstance(v_usage, dict) and isinstance(v_usage.get("total_tokens"), int):
                    rec.tokens = int(v_usage["total_tokens"])
            except (ValueError, OSError):
                pass

        raw_log = relay_dir / f"{rec.step_id}.log"
        if raw_log.is_file():
            rec.raw_log_tail = raw_log.read_text(encoding="utf-8", errors="replace")[-20_000:]
        if raw_log.is_file() and (rec.cost_usd is None or rec.tokens is None):
            raw_txt = raw_log.read_text(encoding="utf-8", errors="replace").strip()
            candidates = [raw_txt] + [
                ln.strip() for ln in reversed(raw_txt.splitlines()) if ln.strip().startswith("{")
            ]
            for chunk in candidates:
                try:
                    parsed = json.loads(chunk)
                except ValueError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                if rec.cost_usd is None and isinstance(parsed.get("total_cost_usd"), (int, float)):
                    rec.cost_usd = float(parsed["total_cost_usd"])
                u_obj = parsed.get("usage")
                if rec.tokens is None and isinstance(u_obj, dict) and isinstance(u_obj.get("total_tokens"), int):
                    rec.tokens = int(u_obj["total_tokens"])
                break

    return rec


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


class HarnessDriver:
    """Executes one (TaskSpec, ContenderConfig, rep) trial inside an isolated worktree & temporary HOME."""

    def __init__(
        self,
        pool: WorktreeSandboxPool,
        upstream_api_url: str | None = None,
        allow_unisolated_host_home: bool = False,
        isolate_home: bool = True,
        auth_copy_mode: bool = False,
        relay_script: str | Path | None = None,
        budget_usd: float | None = None,
        pass_env: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.pool = pool
        self.upstream_api_url = upstream_api_url
        self.allow_unisolated_host_home = allow_unisolated_host_home
        self.isolate_home = isolate_home
        self.auth_copy_mode = auth_copy_mode
        self.relay_script = relay_script
        self.budget_usd = budget_usd
        self.pass_env: tuple[str, ...] = normalize_pass_env_names(pass_env)
        self.cumulative_cost_usd: float = 0.0
        self.max_observed_trial_cost: float = 0.0
        self.completed_trials: int = 0
        self.budget_stopped: bool = False

    def can_start_next_trial(self) -> tuple[bool, float | None]:
        """Check whether another trial can start without exceeding experiment-wide `budget_usd`."""
        if self.budget_usd is None:
            return (True, None)
        remaining = float(self.budget_usd) - self.cumulative_cost_usd
        if remaining <= 1e-9 or (
            self.max_observed_trial_cost > 0.0 and remaining + 1e-9 < self.max_observed_trial_cost
        ):
            self.budget_stopped = True
            return (False, max(0.0, remaining))
        return (True, max(0.0, remaining))

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
        use_isolated_home = bool(self.isolate_home and contender.isolate_home)

        if is_simulated:
            validate_simulation_inputs(
                model=contender.model,
                label=contender.label,
                skill_paths=contender.skill_paths,
            )
        else:
            if base_cli not in SUPPORTED_RELAY_CLIS:
                raise ValueError(
                    f"Unsupported CLI '{contender.cli}': real execution routes exclusively through "
                    f"agent-relay (`relay.sh`), which supports {sorted(SUPPORTED_RELAY_CLIS)}."
                )
            if not use_isolated_home and not (
                self.allow_unisolated_host_home or contender.allow_unisolated_host_home
            ):
                raise PermissionError(
                    "Refusing to launch agent CLI in unisolated host HOME (`--host-home` / `isolate_home=False`) "
                    "without `--allow-unisolated-host-home`."
                )

        verify_test_runner_available(task.test_command)

        trial_id = f"tr_{uuid.uuid4().hex[:10]}"
        slug = f"{task.task_id}__{contender.contender_id}__r{rep_index}"
        started_at = datetime.now(timezone.utc).isoformat()

        trial_home: Path | None = None
        if not is_simulated and use_isolated_home:
            trial_home = Path(tempfile.mkdtemp(prefix="sa-trial-home-"))

        handle = self.pool.acquire(task, slug)
        try:
            mount_report = SkillInjector.apply(
                handle.root,
                contender,
                trial_home=trial_home,
                auth_copy_mode=bool(self.auth_copy_mode or contender.auth_copy_mode),
            )
            exec_outcome: BoundedCommandOutcome | None = None
            if is_simulated:
                usage, steps = self._execute_simulated_contender(handle, task, contender, mount_report, rep_index)
                run_info = AgentRunInfo(wall_ms=int(usage.total_latency_ms))
            else:
                remaining_budget = (
                    max(0.0, float(self.budget_usd) - self.cumulative_cost_usd)
                    if self.budget_usd is not None
                    else None
                )
                usage, steps, exec_outcome, run_info = self._execute_real_cli(
                    handle,
                    task,
                    contender,
                    mount_report,
                    use_isolated_home=use_isolated_home,
                    remaining_budget_usd=remaining_budget,
                )

            trial_effective_cost = max(
                float(usage.total_cost_usd or 0.0),
                float(usage.relay_cost_usd or 0.0),
            )
            self.completed_trials += 1
            self.cumulative_cost_usd = round(self.cumulative_cost_usd + trial_effective_cost, 6)
            if trial_effective_cost > self.max_observed_trial_cost:
                self.max_observed_trial_cost = trial_effective_cost

            try:
                verdict, git_diff = evaluate_sandbox(
                    handle,
                    task,
                    agent_timed_out=bool(exec_outcome and exec_outcome.timed_out),
                    reaped_descendant_pids=list(exec_outcome.reaped_descendant_pids) if exec_outcome else [],
                    unreaped_descendant_pids=list(exec_outcome.unreaped_descendant_pids) if exec_outcome else [],
                    leaked_processes=list(exec_outcome.leaked_processes) if exec_outcome else [],
                )
            except Exception as exc:  # noqa: BLE001 - a grader crash is recorded, not fatal to the experiment
                verdict = GraderVerdict(
                    passed=False,
                    exit_code=1,
                    anti_cheat_clean=True,
                    summary=f"GRADER ERROR: {type(exc).__name__}: {exc}",
                    stdout_tail=cap_grader_output(traceback.format_exc()),
                    failure_kind="grader_error",
                )
                git_diff = ""
            verdict.failure_kind = classify_trial_failure(
                verdict,
                interrupted=is_interrupted(usage.relay_status, verdict.summary),
                is_simulated=is_simulated,
                relay_status=usage.relay_status,
                agent_texts=run_info.agent_texts,
                agent_produced_output=bool(
                    run_info.final_message.strip() or git_diff.strip() or run_info.changed_files
                ),
                relay_wrote_log=run_info.relay_wrote_log,
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
                agent_wall_ms=run_info.wall_ms,
            )
        finally:
            self.pool.release(handle)
            if trial_home is not None and trial_home.exists():
                shutil.rmtree(trial_home, ignore_errors=True)

    def _execute_real_cli(
        self,
        handle: SandboxHandle,
        task: TaskSpec,
        contender: ContenderConfig,
        mount_report: SkillMountReport,
        *,
        use_isolated_home: bool,
        remaining_budget_usd: float | None = None,
    ):
        base_cli = contender.cli.split(":", 1)[0]
        relay_script = resolve_relay_script(self.relay_script)

        upstream = self.upstream_api_url
        if upstream is None:
            if base_cli == "claude":
                upstream = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
            elif base_cli == "codex":
                upstream = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")

        prompt_dir = Path(tempfile.mkdtemp(prefix="sa-prompt-"))
        prompt_file = prompt_dir / "prompt.md"
        prompt_file.write_text(task.prompt, encoding="utf-8")
        trial_tmp = Path(tempfile.mkdtemp(prefix="sa-trial-tmp-"))

        proxy = XRayMeteringBridge(anthropic_upstream=upstream, openai_upstream=upstream)
        proxy.start()
        t0 = time.perf_counter()
        try:
            combined_pass_env = normalize_pass_env_names((*self.pass_env, *contender.pass_env))
            base_env = _build_allowlisted_trial_env(
                mount_report.isolated_env_vars,
                trial_tmp=trial_tmp,
                pass_env=combined_pass_env,
            )
            env = proxy.agent_env(base_env)
            _inject_host_vendor_bin_if_needed(base_cli, env)
            env["RELAY_TIMEOUT"] = str(max(1, int(task.timeout_sec)))
            if base_cli == "claude":
                env["RELAY_CLAUDE_MODEL"] = contender.model
            elif base_cli == "agy":
                env["RELAY_AGY_MODEL"] = contender.model
            effective_budget = remaining_budget_usd if remaining_budget_usd is not None else self.budget_usd
            if effective_budget is not None:
                env["RELAY_BUDGET_USD"] = f"{max(0.0, float(effective_budget)):.4f}"

            unset_keys = ("RELAY_WORKER",)

            cmd = ["/bin/bash", str(relay_script), base_cli, str(handle.root), str(prompt_file)]
            if contender.extra_args:
                cmd.extend(["--", *contender.extra_args])

            outcome = self.pool.run_bounded_command_detailed(
                cmd,
                cwd=handle.root,
                env=env,
                timeout_sec=max(5, int(task.timeout_sec) + 8),
                reap_sandbox_cwd=True,
                unset_env=unset_keys,
                inherit_parent_env=False,
            )

            relay_rec = parse_relay_artifacts(handle.root)
            shutil.rmtree(handle.root / ".relay", ignore_errors=True)

            timed_out = bool(
                outcome.timed_out
                or outcome.returncode == 124
                or (relay_rec.status or "").startswith("timeout")
            )
            outcome.timed_out = timed_out
            rc = 124 if timed_out else outcome.returncode
            stdout = outcome.stdout
            stderr = outcome.stderr
            wall_ms = int((time.perf_counter() - t0) * 1000)

            usage, turn_records = proxy.summarize(injected_skill_tokens_est=mount_report.estimated_tokens)
            if usage.total_latency_ms == 0:
                usage.total_latency_ms = wall_ms
            usage.isolated_home_used = use_isolated_home
            usage.relay_status = relay_rec.status
            usage.relay_cost_usd = relay_rec.cost_usd
            usage.relay_tokens = relay_rec.tokens

            steps: list[TrajectoryStep] = []
            if mount_report.mounted_skill_names:
                steps.append(
                    TrajectoryStep(
                        step_index=1,
                        timestamp_ms=10,
                        kind="skill_mount",
                        summary=f"Mounted skills: {', '.join(mount_report.mounted_skill_names)}",
                        detail=(
                            f"Injected {mount_report.total_injected_chars} chars "
                            f"(~{mount_report.estimated_tokens} tokens) into isolated HOME & workspace."
                        ),
                    )
                )
            for rec in turn_records:
                steps.append(
                    TrajectoryStep(
                        step_index=len(steps) + 1,
                        timestamp_ms=rec.latency_ms,
                        kind="tool_call",
                        summary=f"API Turn #{rec.turn_index} ({rec.model})",
                        detail=(
                            f"Endpoint {rec.endpoint} | prompt={rec.prompt_tokens} "
                            f"(cached={rec.cached_tokens}), out={rec.completion_tokens}, ttft={rec.ttft_ms:.1f}ms"
                        ),
                        prompt_tokens=rec.prompt_tokens or 0,
                        cached_tokens=rec.cached_tokens or 0,
                        completion_tokens=rec.completion_tokens or 0,
                        latency_ms=rec.latency_ms,
                        cost_usd=rec.cost_usd or 0.0,
                    )
                )
            tail = (relay_rec.final_message or stdout or stderr or "").strip()[-900:]
            reap_suffix = ""
            if outcome.reaped_descendant_pids:
                reap_suffix = f" [reaped detached PIDs: {outcome.reaped_descendant_pids}]"
            if outcome.unreaped_descendant_pids:
                reap_suffix += f" [UNREAPED orphan PIDs: {outcome.unreaped_descendant_pids}]"
            relay_tag = f" [relay={relay_rec.status}]" if relay_rec.status else ""
            steps.append(
                TrajectoryStep(
                    step_index=len(steps) + 1,
                    timestamp_ms=wall_ms,
                    kind="thought",
                    summary=f"agent-relay ({base_cli}) exited with code {rc}"
                    + (" (TIMED OUT)" if timed_out else "")
                    + relay_tag
                    + reap_suffix,
                    detail=tail or "Completed headless execution via agent-relay.",
                    latency_ms=wall_ms,
                )
            )
            run_info = AgentRunInfo(
                wall_ms=wall_ms,
                relay_wrote_log=relay_rec.log_md_found,
                final_message=relay_rec.final_message,
                agent_texts=(stderr[-20_000:], stdout[-20_000:], relay_rec.raw_log_tail),
                changed_files=list(relay_rec.changed_files),
            )
            return usage, steps, outcome, run_info
        finally:
            shutil.rmtree(handle.root / ".relay", ignore_errors=True)
            shutil.rmtree(prompt_dir, ignore_errors=True)
            shutil.rmtree(trial_tmp, ignore_errors=True)
            proxy.stop()

    def _execute_simulated_contender(
        self,
        handle: SandboxHandle,
        task: TaskSpec,
        contender: ContenderConfig,
        mount_report: SkillMountReport,
        rep_index: int,
    ):
        """Deterministic offline simulator that edits sandbox files and builds synthetic telemetry."""
        mode = contender.cli.split(":", 1)[1] if ":" in contender.cli else (
            "skilled" if (mount_report.mounted_skill_names or contender.role == "treatment") else "baseline"
        )
        seed_int = int(hashlib.sha256(f"{task.task_id}:{contender.contender_id}:{rep_index}".encode()).hexdigest()[:8], 16)
        skill_tax = mount_report.estimated_tokens if mount_report.estimated_tokens > 0 else (640 if mode == "skilled" else 0)

        num_turns = 3 if mode == "skilled" else 4
        synth_turns: list[tuple[int, int, int, int, float]] = []
        for n in range(1, num_turns + 1):
            base_in = 1820 + (seed_int % 240) + skill_tax + (n - 1) * 540
            cached = 0 if n == 1 else int(base_in * (0.76 if mode == "skilled" else 0.45))
            uncached = max(120, base_in - cached)
            total_p = uncached + cached
            out_tok = (310 if mode == "skilled" else 560) + (seed_int % 90)
            turn_cost = round(
                (uncached / 1_000_000.0) * 3.00
                + (cached / 1_000_000.0) * 0.30
                + (out_tok / 1_000_000.0) * 15.00,
                6,
            )
            synth_turns.append((uncached, cached, total_p, out_tok, turn_cost))

        synth_latencies = [1420 + (seed_int % 380) for _ in synth_turns]
        if mode != "skilled":
            synth_latencies = [2180 + (seed_int % 520) for _ in synth_turns]
        usage = TokenUsageLedger(
            turns=len(synth_turns),
            uncached_input_tokens=sum(t[0] for t in synth_turns),
            first_turn_prompt_tokens=synth_turns[0][2],
            total_prompt_tokens=sum(t[2] for t in synth_turns),
            cached_prompt_tokens=sum(t[1] for t in synth_turns),
            completion_tokens=sum(t[3] for t in synth_turns),
            ttft_ms=float(390 + (seed_int % 140)),
            total_latency_ms=sum(synth_latencies),
            total_cost_usd=round(sum(t[4] for t in synth_turns), 6),
            injected_skill_tokens_est=skill_tax,
            is_metered=True,
            usage_status="simulated",
            is_simulated=True,
        )

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
            for rel_test in task.protected_test_paths[:1]:
                tpath = handle.root / rel_test
                tpath.write_text(
                    "import unittest\nclass Tampered(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
                    encoding="utf-8",
                )
            steps.append(
                TrajectoryStep(
                    step_index=len(steps) + 1,
                    timestamp_ms=920,
                    kind="file_patch",
                    summary=f"[SIMULATED] Modified protected test {task.protected_test_paths[0]}",
                    detail="Overwrote failing assertions in test suite instead of fixing implementation.",
                    prompt_tokens=synth_turns[0][2],
                    completion_tokens=synth_turns[0][3],
                    latency_ms=synth_latencies[0],
                    cost_usd=synth_turns[0][4],
                )
            )
            return usage, steps

        apply_full_solution = mode == "skilled"
        if mode == "baseline":
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

        step_templates = (
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

        for (_, cached_t, prompt_t, out_t, cost_t), lat, (kind, summ, det) in zip(synth_turns, synth_latencies, step_templates):
            elapsed += lat
            steps.append(
                TrajectoryStep(
                    step_index=len(steps) + 1,
                    timestamp_ms=elapsed,
                    kind=kind,
                    summary=f"[SIMULATED] {summ}",
                    detail=det,
                    prompt_tokens=prompt_t,
                    cached_tokens=cached_t,
                    completion_tokens=out_t,
                    latency_ms=lat,
                    cost_usd=cost_t,
                )
            )
        return usage, steps
