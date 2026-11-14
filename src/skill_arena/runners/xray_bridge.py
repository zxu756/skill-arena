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


def _load_agent_xray(repo_root: Path | None = None) -> ModuleType:
    """Import `agent_xray` without hardcoded user paths across direct, sibling, and local-clone setups."""
    if repo_root is None:
        try:
            mod = importlib.import_module("agent_xray")
            if hasattr(mod, "start_background_proxy"):
                return mod
        except ImportError:
            pass

    effective_root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
    candidates: list[Path] = []

    env_src = os.environ.get("AGENT_XRAY_SRC", "").strip()
    if env_src:
        candidates.append(Path(env_src).expanduser().resolve())

    candidates.append((effective_root.parent / "agent-xray" / "src").resolve())
    candidates.extend(_candidate_xray_src_from_git_origin(effective_root))

    for src_dir in candidates:
        init_file = src_dir / "agent_xray" / "__init__.py"
        if init_file.is_file():
            src_str = str(src_dir)
            if src_str not in sys.path:
                sys.path.insert(0, src_str)
            mod = importlib.import_module("agent_xray")
            if hasattr(mod, "start_background_proxy"):
                return mod

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
        self._running = xray.start_background_proxy(
            host="127.0.0.1",
            port=0,
            db_path=":memory:",
            anthropic_upstream=self.anthropic_upstream,
            openai_upstream=self.openai_upstream,
            strip_telemetry_headers=False,
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
