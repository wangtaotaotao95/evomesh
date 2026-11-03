"""Minimal authenticated task API for independently deployed agent instances."""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agent import Agent


def create_server(
    host: str, port: int, token: str, factory: Callable[[], Agent], auto_reflect: bool = False
) -> ThreadingHTTPServer:
    if not token:
        raise ValueError("Set EVOMESH_SERVER_TOKEN before starting the task API")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Do not log task text or request headers.

        def send_json(self, status: int, body: dict):
            payload = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.send_json(
                200 if self.path == "/health" else 404,
                {"status": "ok"} if self.path == "/health" else {"error": "Not found"},
            )

        def do_POST(self):
            if self.path != "/run":
                self.send_json(404, {"error": "Not found"})
                return
            expected = f"Bearer {token}".encode()
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
                self.send_json(401, {"error": "Unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64000 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid body size")
                self.connection.settimeout(15)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                task = data.get("task")
                if not isinstance(task, str) or not task.strip():
                    raise ValueError("task must be a non-empty string")
            except (ValueError, OSError):
                self.send_json(400, {"error": "Expected JSON with a non-empty task; limit 64 KB"})
                return
            agent, status, body = None, 200, {}
            try:
                agent = factory()
                answer = agent.run(task)
                body = {"answer": answer, "instance": agent.instance}
            except Exception:
                status, body = 500, {"error": "Agent run failed; inspect observations"}
            finally:
                body["proposals"] = []
                if auto_reflect and agent is not None and agent.last_observations:
                    try:
                        body["proposals"] = agent.reflect()
                    except Exception:
                        body["reflection_error"] = "Reflection failed; retry with reflect"
            self.send_json(status, body)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
