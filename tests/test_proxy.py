"""Scoped tests for M1 single metering proxy via agent-xray (K7, K8, Responses API, streaming TTFT)."""

from __future__ import annotations

import ast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from skill_arena.runners.xray_bridge import (
    PINNED_AGENT_XRAY_COMMIT,
    XRayMeteringBridge,
    _load_agent_xray,
    get_agent_xray_commit,
)


def test_load_agent_xray_without_hardcoded_paths_and_sole_proxy_in_src(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]

    # 1. Duplicate custom proxy file must be deleted
    assert not (repo_root / "src" / "skill_arena" / "runners" / "wire_tap_proxy.py").exists()

    # 2. Neither runners/ nor sandbox/ may contain a BaseHTTPRequestHandler or ThreadingHTTPServer proxy
    for sub in ("runners", "sandbox"):
        for py_file in (repo_root / "src" / "skill_arena" / sub).rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            banned_names = {"BaseHTTPRequestHandler", "ThreadingHTTPServer", "HTTPServer"}
            found = [
                node.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id in banned_names
            ]
            assert found == [], f"Duplicate HTTP server symbol {found} found in {py_file}"

    # 3. Verify _load_agent_xray() succeeds and exposes start_background_proxy
    xray_mod = _load_agent_xray()
    assert hasattr(xray_mod, "start_background_proxy")

    # 4. Verify Step 4 fallback: when cloned to an isolated tmp directory (where ../agent-xray/src
    #    does NOT exist alongside the clone), _load_agent_xray(repo_root=clone_dir) resolves
    #    agent-xray/src via `git config --get remote.origin.url`!
    isolated_clone = tmp_path / "isolated_nested" / "repo"
    isolated_clone_hop2 = tmp_path / "isolated_hop2" / "repo"
    isolated_clone.parent.mkdir(parents=True, exist_ok=True)
    isolated_clone_hop2.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(repo_root), str(isolated_clone)], check=True)
    subprocess.run(["git", "clone", "-q", str(isolated_clone), str(isolated_clone_hop2)], check=True)
    monkeypatch.delenv("AGENT_XRAY_SRC", raising=False)
    cleaned_sys_path = [p for p in sys.path if "agent-xray" not in p and "agent_xray" not in p]
    monkeypatch.setattr(sys, "path", cleaned_sys_path)
    saved_modules = {k: v for k, v in sys.modules.items() if k == "agent_xray" or k.startswith("agent_xray.")}
    for k in list(saved_modules):
        sys.modules.pop(k, None)
    try:
        loaded_from_origin = _load_agent_xray(repo_root=isolated_clone_hop2)
        assert hasattr(loaded_from_origin, "start_background_proxy")
    finally:
        sys.modules.update(saved_modules)


def test_xray_bridge_streaming_sse_ttft_within_50ms() -> None:
    first_chunk_delays_ms: list[float] = []

    class PacedSSEUpstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            t_req = time.monotonic()
            n = int(self.headers.get("Content-Length", "0"))
            if n > 0:
                self.rfile.read(n)

            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()

            events = [
                b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-5","usage":{"input_tokens":500,"cache_read_input_tokens":100,"output_tokens":0}}}\n\n',
                b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"chunk-1"}}\n\n',
                b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":45}}\n\n',
                b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
            ]
            for idx, ev in enumerate(events):
                time.sleep(0.200)
                if idx == 0:
                    first_chunk_delays_ms.append((time.monotonic() - t_req) * 1000.0)
                self.wfile.write(ev)
                self.wfile.flush()

    up = ThreadingHTTPServer(("127.0.0.1", 0), PacedSSEUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    try:
        up_url = f"http://127.0.0.1:{up.server_address[1]}"
        bridge = XRayMeteringBridge(anthropic_upstream=up_url, openai_upstream=up_url)
        bridge.start()
        try:
            req = urllib.request.Request(
                f"{bridge.anthropic_base_url}/v1/messages",
                data=b'{"model":"claude-sonnet-4-5","stream":true,"messages":[{"role":"user","content":"hi"}]}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                assert b"chunk-1" in raw

            ledger, turns = bridge.summarize(injected_skill_tokens_est=80)
            assert len(turns) == 1
            assert len(first_chunk_delays_ms) == 1
            actual_ttft_ms = first_chunk_delays_ms[0]
            err_ms = abs(float(ledger.ttft_ms) - actual_ttft_ms)
            # Total stream is ~800ms (4 x 200ms); buffered proxy would record ~800ms (error ~600ms).
            assert err_ms <= 50.0, (
                f"Streaming TTFT error {err_ms:.2f}ms exceeded 50ms limit "
                f"(recorded={ledger.ttft_ms:.2f}ms, actual={actual_ttft_ms:.2f}ms, total={ledger.total_latency_ms}ms)"
            )
            assert ledger.total_latency_ms >= 700
            assert ledger.is_metered is True
            assert ledger.uncached_input_tokens == 500
            assert ledger.cached_prompt_tokens == 100
            assert ledger.total_prompt_tokens == 600
            assert ledger.completion_tokens == 45
        finally:
            bridge.stop()
    finally:
        up.shutdown()
        up.server_close()


def test_xray_bridge_openai_responses_api_streaming_and_json_usage() -> None:
    class ResponsesUpstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            raw_req = self.rfile.read(n) if n > 0 else b"{}"
            req_obj = json.loads(raw_req.decode("utf-8"))
            is_stream = bool(req_obj.get("stream"))
            usage_block = {
                "input_tokens": 1200,
                "input_tokens_details": {"cached_tokens": 800},
                "output_tokens": 150,
                "total_tokens": 1350,
            }
            if is_stream:
                sse_body = (
                    'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5"}}\n\n'
                    + "event: response.completed\ndata: "
                    + json.dumps(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_1",
                                "model": "gpt-5",
                                "usage": usage_block,
                            },
                        }
                    )
                    + "\n\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(sse_body)
            else:
                json_body = json.dumps(
                    {
                        "id": "resp_2",
                        "object": "response",
                        "model": "gpt-5",
                        "usage": usage_block,
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(json_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(json_body)

    up = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    try:
        up_url = f"http://127.0.0.1:{up.server_address[1]}"
        bridge = XRayMeteringBridge(openai_upstream=up_url, anthropic_upstream=up_url)
        bridge.start()
        try:
            # Turn 1: Streaming SSE /v1/responses
            req_sse = urllib.request.Request(
                f"{bridge.openai_base_url}/responses",
                data=b'{"model":"gpt-5","stream":true,"input":"fix bug"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req_sse, timeout=5) as r1:
                assert r1.status == 200
                r1.read()

            # Turn 2: Non-streaming JSON /v1/responses
            req_json = urllib.request.Request(
                f"{bridge.openai_base_url}/responses",
                data=b'{"model":"gpt-5","stream":false,"input":"verify fix"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req_json, timeout=5) as r2:
                assert r2.status == 200
                r2.read()

            ledger, turns = bridge.summarize()
            assert len(turns) == 2
            for tr in turns:
                assert tr.has_usage is True
                assert tr.uncached_input_tokens == 400
                assert tr.cached_tokens == 800
                assert tr.prompt_tokens == 1200
                assert tr.completion_tokens == 150
                # gpt-5 pricing in agent-xray: 400*1.25/1e6 + 800*0.125/1e6 + 150*10.0/1e6 = 0.0021 USD
                assert tr.cost_usd == pytest.approx(0.0021, abs=1e-6)

            assert ledger.is_metered is True
            assert ledger.turns == 2
            assert ledger.first_turn_prompt_tokens == 1200
            assert ledger.uncached_input_tokens == 800
            assert ledger.cached_prompt_tokens == 1600
            assert ledger.total_prompt_tokens == 2400
            assert ledger.completion_tokens == 300
            assert ledger.total_cost_usd == pytest.approx(0.0042, abs=1e-6)
        finally:
            bridge.stop()
    finally:
        up.shutdown()
        up.server_close()


def test_json_and_sse_without_usage_record_unmetered_not_zero() -> None:
    class NoUsageUpstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            if n > 0:
                self.rfile.read(n)
            if "/messages" in self.path:
                body = (
                    b'data: {"type":"message_start","message":{"model":"claude-sonnet-4-5"}}\n\n'
                    b'data: {"type":"message_stop"}\n\n'
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                body = json.dumps({"model": "gpt-5", "choices": [{"message": {"content": "hi"}}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    up = ThreadingHTTPServer(("127.0.0.1", 0), NoUsageUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    try:
        up_url = f"http://127.0.0.1:{up.server_address[1]}"
        # 1. JSON response with no usage block
        px_json = XRayMeteringBridge(openai_upstream=up_url, anthropic_upstream=up_url)
        px_json.start()
        try:
            req = urllib.request.Request(
                f"{px_json.openai_base_url}/chat/completions",
                data=b'{"model":"gpt-5","messages":[{"role":"user","content":"hi"}]}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            ledger_j, turns_j = px_json.summarize()
            assert len(turns_j) == 1
            assert turns_j[0].has_usage is False
            assert turns_j[0].prompt_tokens is None
            assert turns_j[0].cost_usd is None
            assert ledger_j.is_metered is False
            assert ledger_j.usage_status == "unmetered"
            assert ledger_j.total_prompt_tokens is None
            assert ledger_j.first_turn_prompt_tokens is None
            assert ledger_j.total_cost_usd is None
        finally:
            px_json.stop()

        # 2. SSE stream with no usage events
        px_sse = XRayMeteringBridge(openai_upstream=up_url, anthropic_upstream=up_url)
        px_sse.start()
        try:
            req2 = urllib.request.Request(
                f"{px_sse.anthropic_base_url}/v1/messages",
                data=b'{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"hi"}]}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req2, timeout=5) as resp2:
                resp2.read()
            ledger_s, turns_s = px_sse.summarize()
            assert len(turns_s) == 1
            assert turns_s[0].has_usage is False
            assert ledger_s.is_metered is False
            assert ledger_s.total_prompt_tokens is None
            assert ledger_s.total_cost_usd is None
        finally:
            px_sse.stop()
    finally:
        up.shutdown()
        up.server_close()


def test_zero_turns_proxy_summarize_reports_unmetered() -> None:
    px = XRayMeteringBridge()
    ledger, turns = px.summarize(injected_skill_tokens_est=150)
    assert turns == []
    assert ledger.turns == 0
    assert ledger.is_metered is False
    assert ledger.usage_status == "unmetered"
    assert ledger.first_turn_prompt_tokens is None
    assert ledger.total_prompt_tokens is None
    assert ledger.total_cost_usd is None


def test_pinned_agent_xray_commit_matches_loaded_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import tomllib

    expected_sha = "3cd9b14dcbae54505c56f982d13fae862d606e5a"
    assert PINNED_AGENT_XRAY_COMMIT == expected_sha

    repo_root = Path(__file__).resolve().parents[1]
    pyproject_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject_data["tool"]["skill_arena"]["pinned_agent_xray_commit"] == expected_sha

    xray_mod = _load_agent_xray()
    captured = capsys.readouterr()
    resolved_sha = get_agent_xray_commit(xray_mod)
    assert resolved_sha == expected_sha
    assert "没核对" not in captured.err and "unverified" not in captured.err

    # Also verify a mismatched git checkout fails pin verification unless overridden with verify_pin=False
    fake_xray_repo = tmp_path / "agent-xray"
    fake_pkg = fake_xray_repo / "src" / "agent_xray"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text(
        "def start_background_proxy(**kwargs):\n    return None\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(fake_xray_repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(fake_xray_repo), "config", "user.name", "T"], check=True)
    subprocess.run(["git", "-C", str(fake_xray_repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(fake_xray_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(fake_xray_repo), "commit", "-q", "-m", "unpinned"], check=True)

    fake_arena_repo = tmp_path / "skill-arena"
    fake_arena_repo.mkdir()
    monkeypatch.delenv("AGENT_XRAY_SRC", raising=False)
    cleaned_sys_path = [p for p in sys.path if "agent-xray" not in p and "agent_xray" not in p]
    monkeypatch.setattr(sys, "path", cleaned_sys_path)
    saved_modules = {k: v for k, v in sys.modules.items() if k == "agent_xray" or k.startswith("agent_xray.")}
    for k in list(saved_modules):
        sys.modules.pop(k, None)
    try:
        with pytest.raises(RuntimeError, match="PINNED_AGENT_XRAY_COMMIT|3cd9b14"):
            _load_agent_xray(repo_root=fake_arena_repo, verify_pin=True)
        for k in list(sys.modules):
            if k == "agent_xray" or k.startswith("agent_xray."):
                sys.modules.pop(k, None)
        unpinned_mod = _load_agent_xray(repo_root=fake_arena_repo, verify_pin=False)
        assert hasattr(unpinned_mod, "start_background_proxy")
    finally:
        for k in list(sys.modules):
            if k == "agent_xray" or k.startswith("agent_xray."):
                sys.modules.pop(k, None)
        sys.modules.update(saved_modules)


def test_kr8_unresolvable_agent_xray_commit_emits_unverified_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """KR8: When agent-xray git SHA cannot be resolved (non-git wheel/copy or failed git rev-parse), emit '没核对' / 'unverified'."""
    wheel_site = tmp_path / "wheel_site"
    pkg_dir = wheel_site / "agent_xray"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text(
        "class _DummyRunning:\n"
        "    base_url = 'http://127.0.0.1:9'\n"
        "    anthropic_base_url = 'http://127.0.0.1:9'\n"
        "    openai_base_url = 'http://127.0.0.1:9'\n"
        "    def stop(self): pass\n"
        "    def get_turns(self, limit=500): return []\n"
        "    def agent_env(self, base_env=None): return dict(base_env or {})\n"
        "def start_background_proxy(**kwargs):\n"
        "    return _DummyRunning()\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_XRAY_SRC", str(wheel_site))
    cleaned_sys_path = [p for p in sys.path if "agent-xray" not in p and "agent_xray" not in p]
    monkeypatch.setattr(sys, "path", cleaned_sys_path)
    saved_modules = {k: v for k, v in sys.modules.items() if k == "agent_xray" or k.startswith("agent_xray.")}
    for k in list(saved_modules):
        sys.modules.pop(k, None)
    try:
        # 1. Non-git directory -> get_agent_xray_commit is None -> warning emitted instead of silent skip
        capsys.readouterr()
        mod = _load_agent_xray(repo_root=tmp_path / "isolated_arena", verify_pin=True)
        err1 = capsys.readouterr().err
        assert get_agent_xray_commit(mod) is None
        assert "没核对" in err1
        assert "unverified" in err1.lower()

        # 2. Bridge start() also surfaces unverified status and emits warning
        bridge = XRayMeteringBridge()
        bridge.start()
        try:
            err2 = capsys.readouterr().err
            assert "没核对" in err2
            assert "unverified" in err2.lower()
            assert bridge.agent_xray_commit is None
            assert bridge.agent_xray_pin_status == "unverified"
        finally:
            bridge.stop()

        # 3. Simulated `git rev-parse` failure (OSError) -> emits '没核对' / 'unverified'
        monkeypatch.setattr(
            "skill_arena.runners.xray_bridge.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("git unavailable")),
        )
        capsys.readouterr()
        _load_agent_xray(verify_pin=True)
        err3 = capsys.readouterr().err
        assert "没核对" in err3
        assert "unverified" in err3.lower()
    finally:
        for k in list(sys.modules):
            if k == "agent_xray" or k.startswith("agent_xray."):
                sys.modules.pop(k, None)
        sys.modules.update(saved_modules)


