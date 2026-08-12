#!/usr/bin/env python3
"""Minimal non-LLM Hermes test double for hosted first-boot tests."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import unquote, urlsplit


_SESSIONS: set[str] = set()
_LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "rvbbit-hermes-fixture/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path == "/health/detailed":
            self._json(
                200,
                {
                    "fixture": True,
                    "status": "degraded",
                    "gateway_state": "stopped",
                    "readiness": {
                        "checks": {
                            "model": {"status": "error"},
                            "config": {"status": "ok"},
                            "gateway": {"status": "error", "state": "stopped"},
                            "memory": {"status": "error"},
                        }
                    },
                },
            )
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/api/sessions":
            self._json(404, {"error": "not_found"})
            return
        try:
            length = min(max(int(self.headers.get("Content-Length", "0")), 0), 16_384)
            payload = json.loads(self.rfile.read(length) or b"{}")
            session_id = str(payload.get("id") or "").strip()
        except Exception:
            session_id = ""
        if not session_id or len(session_id) > 200:
            self._json(400, {"error": "invalid_session"})
            return
        with _LOCK:
            _SESSIONS.add(session_id)
        self._json(201, {"id": session_id, "fixture": True})

    def do_DELETE(self) -> None:  # noqa: N802
        prefix = "/api/sessions/"
        path = urlsplit(self.path).path
        if not path.startswith(prefix):
            self._json(404, {"error": "not_found"})
            return
        session_id = unquote(path[len(prefix) :]).strip()
        with _LOCK:
            existed = session_id in _SESSIONS
            _SESSIONS.discard(session_id)
        self._json(200, {"deleted": existed, "fixture": True})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8642), Handler).serve_forever()
