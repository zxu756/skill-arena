"""Core data contracts for SkillArena tasks, contenders, trajectories, and scoreboards."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class TaskSpec:
    """Isolated, reproducible coding task specification."""

    task_id: str
    title: str
    category: str  # concurrency | frontend_craft | refactor_test | security_hygiene | git_extracted
    prompt: str
    test_command: str
    protected_test_paths: list[str]
    seed_files: dict[str, str] = field(default_factory=dict)
    repo_path: str | None = None
    base_commit: str | None = None
    test_patch: str | None = None
    reference_solution_files: dict[str, str] = field(default_factory=dict)
    naive_attempt_files: dict[str, str] = field(default_factory=dict)
    frontend_target_html: str | None = None
    max_diff_lines: int = 120
    timeout_sec: int = 90

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TaskSpec:
        return cls(
            task_id=str(payload["task_id"]),
            title=str(payload["title"]),
            category=str(payload.get("category", "general")),
            prompt=str(payload["prompt"]),
            test_command=str(payload["test_command"]),
            protected_test_paths=list(payload.get("protected_test_paths", [])),
            seed_files=dict(payload.get("seed_files", {})),
            repo_path=payload.get("repo_path"),
            base_commit=payload.get("base_commit"),
            test_patch=payload.get("test_patch"),
            reference_solution_files=dict(payload.get("reference_solution_files", {})),
            naive_attempt_files=dict(payload.get("naive_attempt_files", {})),
            frontend_target_html=payload.get("frontend_target_html"),
            max_diff_lines=int(payload.get("max_diff_lines", 120)),
            timeout_sec=int(payload.get("timeout_sec", 90)),
        )


@dataclass(slots=True)
class ContenderConfig:
    """Agent CLI + SKILL.md arm configuration (Control or Treatment)."""

    contender_id: str
    label: str
    role: str = "control"  # control | treatment
    cli: str = "claude"  # claude | codex | vibe | agy | simulate
    model: str = "synthetic-sim-model"
    skill_paths: list[str] = field(default_factory=list)
    agents_md_snippet: str | None = None
    strip_default_skills: bool = True
    extra_args: list[str] = field(default_factory=list)
    isolate_home: bool = True
    auth_copy_mode: bool = False
    allow_unisolated_host_home: bool = False
    pass_env: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ContenderConfig:
        return cls(
            contender_id=str(payload["contender_id"]),
            label=str(payload["label"]),
            role=str(payload.get("role", "control")),
            cli=str(payload.get("cli", "claude")),
            model=str(payload.get("model", "synthetic-sim-model")),
            skill_paths=list(payload.get("skill_paths", [])),
            agents_md_snippet=payload.get("agents_md_snippet"),
            strip_default_skills=bool(payload.get("strip_default_skills", True)),
            extra_args=list(payload.get("extra_args", [])),
            isolate_home=bool(payload.get("isolate_home", True)),
            auth_copy_mode=bool(payload.get("auth_copy_mode", False)),
            allow_unisolated_host_home=bool(payload.get("allow_unisolated_host_home", False)),
            pass_env=list(payload.get("pass_env", [])),
        )


@dataclass(slots=True)
class TrajectoryStep:
    """Single recorded step in an agent run for dual-track Studio replay."""

    step_index: int
    timestamp_ms: int
    kind: str  # skill_mount | thought | tool_call | file_patch | verify
    summary: str
    detail: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TrajectoryStep:
        return cls(
            step_index=int(payload["step_index"]),
            timestamp_ms=int(payload.get("timestamp_ms", 0)),
            kind=str(payload.get("kind", "thought")),
            summary=str(payload.get("summary", "")),
            detail=str(payload.get("detail", "")),
            prompt_tokens=int(payload.get("prompt_tokens", 0)),
            cached_tokens=int(payload.get("cached_tokens", 0)),
            completion_tokens=int(payload.get("completion_tokens", 0)),
            latency_ms=int(payload.get("latency_ms", 0)),
            cost_usd=float(payload.get("cost_usd", 0.0)),
        )


@dataclass(slots=True)
class TokenUsageLedger:
    """agent-xray proxy token, TTFT, latency, USD cost, and agent-relay step ledger for one trial."""

    turns: int = 0
    uncached_input_tokens: int | None = None
    first_turn_prompt_tokens: int | None = None
    total_prompt_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    completion_tokens: int | None = None
    ttft_ms: float = 0.0
    total_latency_ms: int = 0
    total_cost_usd: float | None = None
    injected_skill_tokens_est: int = 0
    is_metered: bool = False
    usage_status: str = "unmetered"
    is_simulated: bool = False
    relay_status: str | None = None
    relay_cost_usd: float | None = None
    relay_tokens: int | None = None
    isolated_home_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TokenUsageLedger:
        if "is_metered" in payload:
            metered = bool(payload["is_metered"])
        else:
            metered = payload.get("total_prompt_tokens") is not None and int(payload.get("turns", 0)) > 0
        is_sim = bool(payload.get("is_simulated", False))
        default_status = "simulated" if is_sim else ("metered" if metered else "unmetered")
        return cls(
            turns=int(payload.get("turns", 0)),
            uncached_input_tokens=(
                int(payload["uncached_input_tokens"])
                if metered and payload.get("uncached_input_tokens") is not None
                else None
            ),
            first_turn_prompt_tokens=(
                int(payload["first_turn_prompt_tokens"])
                if metered and payload.get("first_turn_prompt_tokens") is not None
                else None
            ),
            total_prompt_tokens=(
                int(payload["total_prompt_tokens"])
                if metered and payload.get("total_prompt_tokens") is not None
                else None
            ),
            cached_prompt_tokens=(
                int(payload["cached_prompt_tokens"])
                if metered and payload.get("cached_prompt_tokens") is not None
                else None
            ),
            completion_tokens=(
                int(payload["completion_tokens"])
                if metered and payload.get("completion_tokens") is not None
                else None
            ),
            ttft_ms=round(float(payload.get("ttft_ms", 0.0)), 3),
            total_latency_ms=int(payload.get("total_latency_ms", 0)),
            total_cost_usd=(
                float(payload["total_cost_usd"])
                if payload.get("total_cost_usd") is not None and (metered or payload.get("relay_cost_usd") is not None)
                else None
            ),
            injected_skill_tokens_est=int(payload.get("injected_skill_tokens_est", 0)),
            is_metered=metered,
            usage_status=str(payload.get("usage_status", default_status)),
            is_simulated=is_sim,
            relay_status=str(payload["relay_status"]) if payload.get("relay_status") is not None else None,
            relay_cost_usd=float(payload["relay_cost_usd"]) if payload.get("relay_cost_usd") is not None else None,
            relay_tokens=int(payload["relay_tokens"]) if payload.get("relay_tokens") is not None else None,
            isolated_home_used=bool(payload.get("isolated_home_used", False)),
        )


@dataclass(slots=True)
class GraderVerdict:
    """Zero-LLM deterministic grading outcome."""

    passed: bool
    exit_code: int
    anti_cheat_clean: bool
    tampered_test_files: list[str] = field(default_factory=list)
    leaked_processes: list[dict[str, Any]] = field(default_factory=list)
    diff_lines_added: int = 0
    diff_lines_removed: int = 0
    diff_bloat_ratio: float = 0.0
    sloplint_fails: int = 0
    sloplint_warns: int = 0
    summary: str = ""
    stdout_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GraderVerdict:
        return cls(
            passed=bool(payload["passed"]),
            exit_code=int(payload.get("exit_code", 1)),
            anti_cheat_clean=bool(payload.get("anti_cheat_clean", True)),
            tampered_test_files=list(payload.get("tampered_test_files", [])),
            leaked_processes=[dict(item) for item in payload.get("leaked_processes", [])],
            diff_lines_added=int(payload.get("diff_lines_added", 0)),
            diff_lines_removed=int(payload.get("diff_lines_removed", 0)),
            diff_bloat_ratio=float(payload.get("diff_bloat_ratio", 0.0)),
            sloplint_fails=int(payload.get("sloplint_fails", 0)),
            sloplint_warns=int(payload.get("sloplint_warns", 0)),
            summary=str(payload.get("summary", "")),
            stdout_tail=str(payload.get("stdout_tail", "")),
        )


@dataclass(slots=True)
class TrialResult:
    """Complete execution artifact for a single (task, contender, repetition) run."""

    trial_id: str
    experiment_id: str
    task_id: str
    task_title: str
    task_category: str
    contender_id: str
    contender_label: str
    contender_role: str
    rep_index: int
    passed: bool
    anti_cheat_clean: bool
    usage: TokenUsageLedger
    verdict: GraderVerdict
    steps: list[TrajectoryStep]
    git_diff: str
    started_at: str
    finished_at: str
    is_simulated: bool = False

    @property
    def leaked_processes(self) -> list[dict[str, Any]]:
        return self.verdict.leaked_processes

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "experiment_id": self.experiment_id,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "task_category": self.task_category,
            "contender_id": self.contender_id,
            "contender_label": self.contender_label,
            "contender_role": self.contender_role,
            "rep_index": self.rep_index,
            "passed": self.passed,
            "anti_cheat_clean": self.anti_cheat_clean,
            "leaked_processes": list(self.verdict.leaked_processes),
            "metered_cost_usd": self.usage.total_cost_usd if self.usage.is_metered else None,
            "relay_cost_usd": self.usage.relay_cost_usd,
            "usage": self.usage.to_dict(),
            "verdict": self.verdict.to_dict(),
            "steps": [s.to_dict() for s in self.steps],
            "git_diff": self.git_diff,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "is_simulated": self.is_simulated,
        }


@dataclass(slots=True)
class LeaderboardSummary:
    """Aggregated scientific benchmark metrics for one contender arm."""

    contender_id: str
    label: str
    role: str
    cli: str
    model: str
    skills_mounted: list[str]
    trials_total: int
    trials_passed: int
    tasks_total: int
    pass_at_1: float
    pass_hat_k: float
    k_reps: int
    total_spend_usd: float | None
    cost_per_success_usd: float | None
    mean_first_turn_tokens: int | None
    context_overhead_delta_tokens: int | None
    median_latency_ms: int
    anti_cheat_integrity_rate: float
    elo_rating: int = 1500
    rank: int | None = None
    isolate_home: bool = True
    is_metered: bool = False
    usage_status: str = "unmetered"
    is_simulated: bool = False
    metered_cost_usd: float | None = None
    total_relay_cost_usd: float | None = None
    relay_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
