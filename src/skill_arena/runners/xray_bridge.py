"""Single programmatic LLM proxy metering bridge backed exclusively by agent-xray."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Any
import urllib.parse

from skill_arena.models import TokenUsageLedger

PINNED_AGENT_XRAY_COMMIT = "7856d178c4b8ffea5dff52309497739d140ff7f1"


def get_agent_xray_commit(target: ModuleType | Path | None = None) -> str | None:
    """Return the 40-char git HEAD commit when `agent_xray` is loaded from an `agent-xray` git checkout."""
    probe_path: Path | None = None
    if target is None:
        mod = _load_agent_xray(verify_pin=False)
        mod_file = getattr(mod, "__file__", None)
        if mod_file:
            probe_path = Path(mod_file).resolve()
    elif isinstance(target, ModuleType):
        mod_file = getattr(target, "__file__", None)
        if mod_file:
            probe_path = Path(mod_file).resolve()
    else:
        probe_path = Path(target).resolve()

    if probe_path is None:
        return None
    probe_dir = probe_path if probe_path.is_dir() else probe_path.parent
    if not probe_dir.exists():
        return None

    try:
        top_proc = subprocess.run(
            ["git", "-C", str(probe_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if top_proc.returncode != 0:
            return None
        toplevel = Path(top_proc.stdout.strip()).resolve()
        checkout_pkg = (toplevel / "src" / "agent_xray").resolve()
        if not (checkout_pkg / "__init__.py").is_file():
            return None
        if not (probe_dir == toplevel or probe_dir == toplevel / "src" or probe_dir.is_relative_to(checkout_pkg)):
            return None
        sha_proc = subprocess.run(
            ["git", "-C", str(toplevel), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if sha_proc.returncode != 0:
            return None
        sha = sha_proc.stdout.strip()
        return sha or None
    except (OSError, subprocess.SubprocessError):
        return None


def _verify_agent_xray_pin(mod: ModuleType, *, verify_pin: bool) -> ModuleType:
    if not verify_pin:
        return mod
    actual_sha = get_agent_xray_commit(mod)
    if actual_sha is None:
        print(
            "[WARNING] agent-xray commit unverified / agent-xray 提交号没核对 "
            f"(expected PINNED_AGENT_XRAY_COMMIT={PINNED_AGENT_XRAY_COMMIT}; git commit SHA unavailable).",
            file=sys.stderr,
        )
        return mod
    if actual_sha != PINNED_AGENT_XRAY_COMMIT:
        raise RuntimeError(
            f"Loaded agent-xray git checkout is at {actual_sha}, expected "
            f"PINNED_AGENT_XRAY_COMMIT={PINNED_AGENT_XRAY_COMMIT}."
        )
    return mod


def _candidate_xray_src_from_git_origin(repo_root: Path, *, max_hops: int = 5) -> list[Path]:
    """Follow local `remote.origin.url` chain (cycle-guarded) to locate sibling `agent-xray/src`."""
    candidates: list[Path] = []
    visited: set[Path] = set()
    current = repo_root.resolve()

    for _ in range(max_hops):
        if current in visited or not current.exists():
            break
        visited.add(current)
        try:
            proc = subprocess.run(
                ["git", "-C", str(current), "config", "--get", "remote.origin.url"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except OSError:
            break
        origin = proc.stdout.strip()
        if not origin:
            break
        if origin.startswith("file://"):
            parsed = urllib.parse.urlsplit(origin)
            origin = urllib.parse.unquote(parsed.path)
        if "://" in origin or origin.startswith("git@"):
            break
        origin_path = Path(origin).expanduser()
        if not origin_path.is_absolute():
            origin_path = (current / origin_path).resolve()
        else:
            origin_path = origin_path.resolve()
        candidates.append(origin_path.parent / "agent-xray" / "src")
        if origin_path.name == ".git":
            candidates.append(origin_path.parent.parent / "agent-xray" / "src")
            current = origin_path.parent
        else:
            current = origin_path

    return candidates


def _load_agent_xray(
    repo_root: Path | None = None,
    *,
    verify_pin: bool = True,
) -> ModuleType:
    """Import `agent_xray` without hardcoded user paths across direct, sibling, and local-clone setups."""
    env_src = os.environ.get("AGENT_XRAY_SRC", "").strip()
    if repo_root is None and not env_src:
        try:
            mod = importlib.import_module("agent_xray")
            if hasattr(mod, "start_background_proxy"):
                return _verify_agent_xray_pin(mod, verify_pin=verify_pin)
        except ImportError:
            pass

    effective_root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
    candidates: list[Path] = []

    if env_src:
        candidates.append(Path(env_src).expanduser().resolve())

    candidates.append((effective_root.parent / "agent-xray" / "src").resolve())
    candidates.extend(_candidate_xray_src_from_git_origin(effective_root))

    for src_dir in candidates:
        init_file = src_dir / "agent_xray" / "__init__.py"
        if init_file.is_file():
            src_str = str(src_dir)
            if src_str in sys.path:
                sys.path.remove(src_str)
            sys.path.insert(0, src_str)
            existing = sys.modules.get("agent_xray")
            existing_file = getattr(existing, "__file__", None) if existing is not None else None
            if existing_file is not None and not Path(existing_file).resolve().is_relative_to(src_dir.resolve()):
                for mod_name in [k for k in sys.modules if k == "agent_xray" or k.startswith("agent_xray.")]:
                    sys.modules.pop(mod_name, None)
            mod = importlib.import_module("agent_xray")
            if hasattr(mod, "start_background_proxy"):
                return _verify_agent_xray_pin(mod, verify_pin=verify_pin)

    raise ImportError(
        "Unable to locate `agent_xray`: install `agent-xray`, set `AGENT_XRAY_SRC` to `agent-xray/src`, "
        "or place `agent-xray` as a sibling repository."
    )


@dataclass(slots=True)
class XRayTurnRecord:
    """Normalized per-turn telemetry record from agent-xray TraceStore."""

    turn_index: int
    endpoint: str
    provider: str
    model: str
    uncached_input_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    ttft_ms: float
    latency_ms: int
    cost_usd: float | None
    has_usage: bool = True


class XRayMeteringBridge:
    """Lifecycle wrapper around `agent_xray.start_background_proxy` for one trial."""

    def __init__(
        self,
        *,
        anthropic_upstream: str | None = None,
        openai_upstream: str | None = None,
    ) -> None:
        self.anthropic_upstream = (
            anthropic_upstream
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com"
        )
        self.openai_upstream = (
            openai_upstream
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com"
        )
        self._running: Any | None = None
        self.agent_xray_commit: str | None = None
        self.agent_xray_pin_status: str = "unverified"

    @property
    def base_url(self) -> str:
        if self._running is None:
            raise RuntimeError("XRayMeteringBridge has not been started")
        return str(self._running.base_url)

    @property
    def anthropic_base_url(self) -> str:
        if self._running is None:
            raise RuntimeError("XRayMeteringBridge has not been started")
        return str(self._running.anthropic_base_url)

    @property
    def openai_base_url(self) -> str:
        if self._running is None:
            raise RuntimeError("XRayMeteringBridge has not been started")
        return str(self._running.openai_base_url)

    def start(self) -> str:
        xray = _load_agent_xray()
        self.agent_xray_commit = get_agent_xray_commit(xray)
        self.agent_xray_pin_status = (
            "verified" if self.agent_xray_commit == PINNED_AGENT_XRAY_COMMIT else "unverified"
        )
        self._running = xray.start_background_proxy(
            host="127.0.0.1",
            port=0,
            db_path=":memory:",
            anthropic_upstream=self.anthropic_upstream,
            openai_upstream=self.openai_upstream,
            strip_telemetry=False,
            block_secrets=False,
            store_previews=False,
        )
        return self.base_url

    def agent_env(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        if self._running is None:
            raise RuntimeError("XRayMeteringBridge has not been started")
        merged = dict(self._running.agent_env(base_env))
        existing_no_proxy = (
            merged.get("NO_PROXY")
            or merged.get("no_proxy")
            or os.environ.get("NO_PROXY")
            or os.environ.get("no_proxy")
            or ""
        )
        parts = [p.strip() for p in existing_no_proxy.split(",") if p.strip()]
        for loop_host in ("127.0.0.1", "localhost", "::1"):
            if loop_host not in parts:
                parts.append(loop_host)
        joined = ",".join(parts)
        merged["NO_PROXY"] = joined
        merged["no_proxy"] = joined
        return merged

    def stop(self) -> None:
        if self._running is not None:
            self._running.stop()
            self._running = None

    def summarize(
        self,
        *,
        injected_skill_tokens_est: int = 0,
        relay_status: str | None = None,
        relay_cost_usd: float | None = None,
        relay_tokens: int | None = None,
        isolated_home_used: bool = False,
    ) -> tuple[TokenUsageLedger, list[XRayTurnRecord]]:
        traces: list[dict[str, Any]] = (
            list(self._running.get_turns(limit=500)) if self._running is not None else []
        )
        records: list[XRayTurnRecord] = []
        for idx, t in enumerate(traces, start=1):
            u = t.get("usage") or {}
            in_uncached = int(u.get("input_tokens", 0))
            in_cache_read = int(u.get("cache_read_tokens", 0))
            in_cache_write = int(u.get("cache_write_tokens", 0))
            out_tok = int(u.get("output_tokens", 0))
            status_code = int(t.get("status_code", 0))
            blocked = bool(t.get("blocked", False))

            has_usage = (
                (not blocked)
                and (200 <= status_code < 400)
                and (in_uncached > 0 or in_cache_read > 0 or in_cache_write > 0 or out_tok > 0)
            )
            total_prompt = (in_uncached + in_cache_read + in_cache_write) if has_usage else None
            ttft_val = round(float(t["ttft_ms"]), 3) if t.get("ttft_ms") is not None else 0.0
            latency_ms = int(t.get("latency_ms", 0))
            est_cost = (
                float(t["estimated_cost_usd"])
                if (has_usage and t.get("estimated_cost_usd") is not None)
                else None
            )
            records.append(
                XRayTurnRecord(
                    turn_index=idx,
                    endpoint=str(t.get("path", "")),
                    provider=str(t.get("provider", "")),
                    model=str(t.get("model", "")),
                    uncached_input_tokens=in_uncached if has_usage else None,
                    cached_tokens=in_cache_read if has_usage else None,
                    cache_write_tokens=in_cache_write if has_usage else None,
                    prompt_tokens=total_prompt,
                    completion_tokens=out_tok if has_usage else None,
                    ttft_ms=ttft_val,
                    latency_ms=latency_ms,
                    cost_usd=est_cost,
                    has_usage=has_usage,
                )
            )

        if not records or not all(r.has_usage for r in records):
            return (
                TokenUsageLedger(
                    turns=len(records),
                    uncached_input_tokens=None,
                    first_turn_prompt_tokens=None,
                    total_prompt_tokens=None,
                    cached_prompt_tokens=None,
                    completion_tokens=None,
                    ttft_ms=records[0].ttft_ms if records else 0.0,
                    total_latency_ms=sum(r.latency_ms for r in records),
                    total_cost_usd=None,
                    injected_skill_tokens_est=injected_skill_tokens_est,
                    is_metered=False,
                    usage_status="unmetered",
                    relay_status=relay_status,
                    relay_cost_usd=relay_cost_usd,
                    relay_tokens=relay_tokens,
                    isolated_home_used=isolated_home_used,
                ),
                records,
            )

        all_turn_costs_known = all(r.cost_usd is not None for r in records)
        total_cost: float | None = (
            round(sum(float(r.cost_usd or 0.0) for r in records), 6)
            if all_turn_costs_known
            else (round(relay_cost_usd, 6) if relay_cost_usd is not None else None)
        )
        return (
            TokenUsageLedger(
                turns=len(records),
                uncached_input_tokens=sum(int(r.uncached_input_tokens or 0) for r in records),
                first_turn_prompt_tokens=int(records[0].prompt_tokens or 0),
                total_prompt_tokens=sum(int(r.prompt_tokens or 0) for r in records),
                cached_prompt_tokens=sum(int(r.cached_tokens or 0) for r in records),
                completion_tokens=sum(int(r.completion_tokens or 0) for r in records),
                ttft_ms=records[0].ttft_ms,
                total_latency_ms=sum(r.latency_ms for r in records),
                total_cost_usd=total_cost,
                injected_skill_tokens_est=injected_skill_tokens_est,
                is_metered=True,
                usage_status="metered",
                relay_status=relay_status,
                relay_cost_usd=relay_cost_usd,
                relay_tokens=relay_tokens,
                isolated_home_used=isolated_home_used,
            ),
            records,
        )
