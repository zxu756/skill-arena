"""Local HTTP Studio server exposing SQLite benchmark APIs and live dual-track UI."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import parse_qs, urlparse

from skill_arena.store.arena_db import ArenaDatabase
from skill_arena.studio.templates import render_arena_html


def create_studio_server(db: ArenaDatabase, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer serving the SkillArena Studio web app and JSON endpoints."""

    class StudioHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)

            if parsed.path == "/api/experiments":
                self._send_json(200, {"experiments": db.list_experiments()})
                return

            if parsed.path.startswith("/api/comparison/"):
                exp_id = parsed.path.split("/api/comparison/", 1)[1].strip("/")
                try:
                    payload = db.build_comparison_report(exp_id)
                    self._send_json(200, payload)
                except KeyError:
                    self._send_json(404, {"error": f"Unknown experiment: {exp_id}"})
                return

            exp_id = (qs.get("exp") or [None])[0] or db.latest_experiment_id()
            if exp_id is None:
                body = b"<!doctype html><title>SkillArena Studio</title><p>No experiments found in database.</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            default_view = "replay" if (qs.get("view") or [""])[0] == "replay" else "scoreboard"
            html_doc = render_arena_html(db.build_comparison_report(exp_id), default_view=default_view)
            encoded = html_doc.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, status: int, data: dict) -> None:
            raw = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

    return ThreadingHTTPServer((host, port), StudioHandler)
