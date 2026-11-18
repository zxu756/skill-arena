"""Behaviour metrics from an agent's tool calls: test runs, test-first, and whether the skill was read.

agent-relay's own output carries no tool calls (claude prints one JSON result, codex and agy a final
message). Mistral Vibe is the exception: relay saves vibe's ``--output json`` message list as
``.relay/<step>.vibe/messages.json``, where every tool call is an ``effect`` entry with
``detail.toolName`` (``file_system.bash``, ``file_system.read_file``, ``file_system.write_file``,
``file_system.edit``) and ``detail.input`` (``command`` or ``filePath``). For other CLIs the metrics
stay ``None``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from skill_arena.models import TrajectoryStep

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?/|test_[^/]+\.py$|[^/]+_test\.(py|go|ts|tsx|js|rs)$|[^/]+\.(spec|test)\.(ts|tsx|js|jsx|mjs)$|"
    r"conftest\.py$)"
)
_TEST_RUN_RE = re.compile(
    r"\b(?:py\.?test|python[0-9.]*\s+-m\s+(?:pytest|unittest)|unittest\b|npm\s+(?:run\s+)?test|"
    r"node\s+--test|go\s+test|cargo\s+test|jest|vitest)\b"
)
_HARNESS_FILES = frozenset({"AGENTS.md", "CLAUDE.md"})
MAX_TOOL_STEPS = 200


def _rel_to_workspace(raw_path: str, workspace: Path) -> str | None:
    if not raw_path:
        return None
    ws = os.path.realpath(str(workspace))
    p = raw_path if os.path.isabs(raw_path) else os.path.join(ws, raw_path)
    real = os.path.realpath(p)
    if real == ws or not real.startswith(ws + os.sep):
        return None
    return os.path.relpath(real, ws).replace(os.sep, "/")


def load_vibe_tool_calls(messages_path: Path) -> list[dict[str, Any]] | None:
    """Return vibe's tool calls in order as ``{"tool", "command", "path"}``, or None if unreadable."""
    try:
        data = json.loads(messages_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    calls: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("type") != "effect":
            continue
        detail = entry.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        tool = str(detail.get("toolName") or entry.get("title") or "")
        inp = detail.get("input") or {}
        if not isinstance(inp, dict):
            inp = {}
        state = entry.get("state") or {}
        denied = isinstance(state, dict) and (state.get("error") or {}).get("code") == "tool_denied"
        calls.append(
            {
                "tool": tool,
                "command": str(inp.get("command") or ""),
                "path": str(inp.get("filePath") or inp.get("path") or ""),
                "denied": bool(denied),
                "created_at": entry.get("createdAt"),
            }
        )
    return calls


def behavior_from_tool_calls(
    calls: list[dict[str, Any]],
    workspace: Path,
    *,
    skill_names: list[str],
    test_command: str = "",
) -> dict[str, Any]:
    """Compute test_runs, test_first and skill_loaded from ordered tool calls."""
    test_runs = 0
    first_write: str | None = None  # "test" | "impl"
    skill_loaded = False
    test_cmd = test_command.strip()
    for call in calls:
        if call.get("denied"):
            continue
        tool = str(call.get("tool", ""))
        cmd = str(call.get("command", ""))
        path = str(call.get("path", ""))
        short = tool.rsplit(".", 1)[-1]
        if short == "bash":
            if _TEST_RUN_RE.search(cmd) or (test_cmd and test_cmd in cmd):
                test_runs += 1
            if "SKILL.md" in cmd and any(name in cmd for name in skill_names):
                skill_loaded = True
        elif short in {"read_file", "read"}:
            if path.endswith("SKILL.md") and any(f"/{name}/" in path for name in skill_names):
                skill_loaded = True
        elif short in {"write_file", "edit", "write", "multi_edit"}:
            rel = _rel_to_workspace(path, workspace)
            if rel is None or rel in _HARNESS_FILES or rel.startswith((".relay/", ".agents/", ".claude/")):
                continue
            kind = "test" if _TEST_PATH_RE.search(rel) else "impl"
            if first_write is None:
                first_write = kind
    return {
        "test_runs": test_runs,
        "test_first": None if first_write is None else first_write == "test",
        "skill_loaded": skill_loaded if skill_names else None,
        "source": "vibe messages.json",
        "tool_calls": len(calls),
    }


def tool_call_steps(calls: list[dict[str, Any]], start_index: int, base_ms: int = 0) -> list[TrajectoryStep]:
    """Replay steps for the first ``MAX_TOOL_STEPS`` tool calls."""
    steps: list[TrajectoryStep] = []
    first_ts = next((c.get("created_at") for c in calls if isinstance(c.get("created_at"), (int, float))), None)
    for i, call in enumerate(calls[:MAX_TOOL_STEPS]):
        short = str(call.get("tool", "")).rsplit(".", 1)[-1]
        arg = call.get("command") or call.get("path") or ""
        ts = call.get("created_at")
        rel_ms = int(ts - first_ts) if isinstance(ts, (int, float)) and first_ts is not None else 0
        steps.append(
            TrajectoryStep(
                step_index=start_index + i,
                timestamp_ms=base_ms + max(0, rel_ms),
                kind="file_patch" if short in {"write_file", "edit", "write", "multi_edit"} else "tool_call",
                summary=f"{short}: {str(arg)[:120]}" + (" (denied)" if call.get("denied") else ""),
                detail=str(arg)[:2000],
            )
        )
    return steps


UNAVAILABLE_BEHAVIOR: dict[str, Any] = {
    "test_runs": None,
    "test_first": None,
    "skill_loaded": None,
}
