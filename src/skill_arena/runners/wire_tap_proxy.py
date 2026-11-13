"""Zero-certificate local HTTP loopback wire-tap proxy for token, cache, TTFT, and USD telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from skill_arena.models import TokenUsageLedger

# Per-1M token pricing table (USD): (input_per_m, cached_input_per_m, output_per_m)
MODEL_PRICING_PER_M: dict[str, tuple[float, float, float]] = {
    "claude-opus-4-6": (15.00, 1.50, 75.00),
    "claude-sonnet-4-5": (3.00, 0.30, 15.00),
    "claude-haiku-4-5": (0.80, 0.08, 4.00),
    "gpt-5": (2.50, 0.25, 10.00),
    "gpt-5-codex": (2.50, 0.25, 10.00),
    "codex-mini": (1.50, 0.375, 6.00),
    "gemini-2.5-pro": (1.25, 0.31, 10.00),
    "mistral-medium-3": (0.40, 0.10, 2.00),
}


def compute_turn_cost_usd(
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
) -> float:
    prices = MODEL_PRICING_PER_M.get(model)
    if prices is None:
        for prefix, candidate in MODEL_PRICING_PER_M.items():
            if model.startswith(prefix):
                prices = candidate
                break
    if prices is None:
        prices = (3.00, 0.30, 15.00)

    in_rate, cache_rate, out_rate = prices
    uncached_prompt = max(0, prompt_tokens - cached_tokens)
    cost = (
        (uncached_prompt / 1_000_000.0) * in_rate
        + (max(0, cached_tokens) / 1_000_000.0) * cache_rate
        + (max(0, completion_tokens) / 1_000_000.0) * out_rate
    )
    return round(cost, 6)


@dataclass(slots=True)
class WireTapTurnRecord:
    turn_index: int
    endpoint: str
    model: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    ttft_ms: int
    latency_ms: int
    cost_usd: float


def _extract_usage_from_json(payload: dict[str, Any], default_model: str) -> tuple[str, int, int, int]:
    model = str(payload.get("model") or default_model)
    usage = payload.get("usage") or {}
    # Anthropic format: input_tokens, cache_read_input_tokens, cache_creation_input_tokens, output_tokens
    if "input_tokens" in usage or "output_tokens" in usage:
        uncached = int(usage.get("input_tokens", 0))
        cache_read = int(usage.get("cache_read_input_tokens", 0))
        cache_create = int(usage.get("cache_creation_input_tokens", 0))
        total_in = uncached + cache_read + cache_create
        out_tok = int(usage.get("output_tokens", 0))
        return model, total_in, cache_read, out_tok

    # OpenAI / Responses format: prompt_tokens, prompt_tokens_details.cached_tokens, completion_tokens
    total_in = int(usage.get("prompt_tokens", 0))
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = int(details.get("cached_tokens", 0))
    out_tok = int(usage.get("completion_tokens", 0))
    return model, total_in, cached, out_tok


def _extract_usage_from_sse(raw_body: bytes, default_model: str) -> tuple[str, int, int, int]:
    model = default_model
    total_in = 0
    cached = 0
    out_tok = 0
    text = raw_body.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            msg = event.get("message") if isinstance(event.get("message"), dict) else event
            if msg.get("model"):
                model = str(msg["model"])
            if "usage" in event or "usage" in msg:
                target = event if "usage" in event else msg
                m, p_in, p_cache, p_out = _extract_usage_from_json(target, model)
                model = m
                if p_in > 0:
                    total_in = p_in
                if p_cache > 0:
                    cached = p_cache
                if p_out > 0:
                    out_tok = p_out
    return model, total_in, cached, out_tok


class LoopbackWireTapProxy:
    """Thread-safe local HTTP reverse proxy that meters LLM API calls without TLS MITM."""

    def __init__(
        self,
        upstream_base_url: str | None = None,
        default_model: str = "claude-sonnet-4-5",
        synthetic_responder: Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]] | None = None,
    ) -> None:
        self.upstream_base_url = upstream_base_url.rstrip("/") if upstream_base_url else None
        self.default_model = default_model
        self.synthetic_responder = synthetic_responder
        self._lock = threading.Lock()
        self._turns: list[WireTapTurnRecord] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("WireTap proxy has not been started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> str:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                t0 = time.perf_counter()
                length = int(self.headers.get("Content-Length", "0"))
                req_bytes = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    req_json = json.loads(req_bytes.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    req_json = {}

                req_model = str(req_json.get("model") or proxy.default_model)

                if proxy.synthetic_responder is not None:
                    status, resp_dict = proxy.synthetic_responder(self.path, req_json)
                    ttft_ms = max(1, int((time.perf_counter() - t0) * 1000))
                    resp_bytes = json.dumps(resp_dict).encode("utf-8")
                    latency_ms = max(ttft_ms, int((time.perf_counter() - t0) * 1000))
                    model, p_tok, c_tok, o_tok = _extract_usage_from_json(resp_dict, req_model)
                    proxy._record_turn(self.path, model, p_tok, c_tok, o_tok, ttft_ms, latency_ms)
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp_bytes)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(resp_bytes)
                    self.close_connection = True
                    return

                if proxy.upstream_base_url is None:
                    err = json.dumps({"error": "No upstream_base_url configured on LoopbackWireTapProxy"}).encode()
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return

                target_url = f"{proxy.upstream_base_url}{self.path}"
                fwd_headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}
                }
                fwd_headers["Accept-Encoding"] = "identity"
                upstream_req = urllib.request.Request(target_url, data=req_bytes, headers=fwd_headers, method="POST")

                chunks: list[bytes] = []
                ttft_ms = 0
                status = 200
                resp_content_type = "application/json"
                try:
                    with urllib.request.urlopen(upstream_req, timeout=120) as up_resp:
                        status = up_resp.status
                        resp_content_type = up_resp.headers.get("Content-Type", "application/json")
                        self.send_response(status)
                        self.send_header("Content-Type", resp_content_type)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        while True:
                            chunk = up_resp.read(4096)
                            if not chunk:
                                break
                            if ttft_ms == 0:
                                ttft_ms = max(1, int((time.perf_counter() - t0) * 1000))
                            chunks.append(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    err_body = exc.read()
                    ttft_ms = max(1, int((time.perf_counter() - t0) * 1000))
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err_body)))
                    self.end_headers()
                    self.wfile.write(err_body)
                    chunks.append(err_body)

                latency_ms = max(ttft_ms, int((time.perf_counter() - t0) * 1000))
                full_body = b"".join(chunks)
                if "text/event-stream" in resp_content_type:
                    model, p_tok, c_tok, o_tok = _extract_usage_from_sse(full_body, req_model)
                else:
                    try:
                        parsed = json.loads(full_body.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        parsed = {}
                    model, p_tok, c_tok, o_tok = _extract_usage_from_json(parsed, req_model)
                proxy._record_turn(self.path, model, p_tok, c_tok, o_tok, ttft_ms, latency_ms)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _record_turn(
        self,
        endpoint: str,
        model: str,
        prompt_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
        ttft_ms: int,
        latency_ms: int,
    ) -> None:
        with self._lock:
            idx = len(self._turns) + 1
            cost = compute_turn_cost_usd(model, prompt_tokens, cached_tokens, completion_tokens)
            self._turns.append(
                WireTapTurnRecord(
                    turn_index=idx,
                    endpoint=endpoint,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    cached_tokens=cached_tokens,
                    completion_tokens=completion_tokens,
                    ttft_ms=ttft_ms,
                    latency_ms=latency_ms,
                    cost_usd=cost,
                )
            )

    def summarize(self, injected_skill_tokens_est: int = 0) -> tuple[TokenUsageLedger, list[WireTapTurnRecord]]:
        with self._lock:
            records = list(self._turns)
        if not records:
            return TokenUsageLedger(injected_skill_tokens_est=injected_skill_tokens_est), []
        return (
            TokenUsageLedger(
                turns=len(records),
                first_turn_prompt_tokens=records[0].prompt_tokens,
                total_prompt_tokens=sum(r.prompt_tokens for r in records),
                cached_prompt_tokens=sum(r.cached_tokens for r in records),
                completion_tokens=sum(r.completion_tokens for r in records),
                ttft_ms=records[0].ttft_ms,
                total_latency_ms=sum(r.latency_ms for r in records),
                total_cost_usd=round(sum(r.cost_usd for r in records), 6),
                injected_skill_tokens_est=injected_skill_tokens_est,
            ),
            records,
        )
