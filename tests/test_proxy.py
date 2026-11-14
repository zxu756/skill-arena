"""Scoped tests for K8/K9 proxy metering honesty: missing upstream usage and 0-turn CLIs must report Unmetered (None), never 0."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.request

from skill_arena.runners.wire_tap_proxy import LoopbackWireTapProxy


def test_json_and_sse_without_usage_record_unmetered_not_zero() -> None:
    class NoUsageUpstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            if n > 0:
                self.rfile.read(n)
            if self.path == "/sse":
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
        # 1. JSON response with no usage block
        px_json = LoopbackWireTapProxy(upstream_base_url=f"http://127.0.0.1:{up.server_address[1]}")
        url_json = px_json.start()
        try:
            req = urllib.request.Request(
                f"{url_json}/v1/chat/completions",
                data=b'{"model":"gpt-5"}',
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
        px_sse = LoopbackWireTapProxy(upstream_base_url=f"http://127.0.0.1:{up.server_address[1]}")
        url_sse = px_sse.start()
        try:
            req2 = urllib.request.Request(
                f"{url_sse}/sse",
                data=b'{"model":"claude-sonnet-4-5"}',
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
    px = LoopbackWireTapProxy(upstream_base_url=None)
    ledger, turns = px.summarize(injected_skill_tokens_est=150)
    assert turns == []
    assert ledger.turns == 0
    assert ledger.is_metered is False
    assert ledger.usage_status == "unmetered"
    assert ledger.first_turn_prompt_tokens is None
    assert ledger.total_prompt_tokens is None
    assert ledger.total_cost_usd is None
