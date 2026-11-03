"""Authenticated API shared by personal assistants and embedded agent applications."""

from __future__ import annotations

import hmac
import json
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .agent import Agent
from .app import AgentApp
from .runtime_store import SessionBusy


def public_task(row):
    return (
        {k: v for k, v in row.items() if k not in {"token", "dedupe", "lease_until"}}
        if row
        else None
    )


def create_server(
    host: str,
    port: int,
    token: str,
    factory: Callable[[], Agent],
    auto_reflect: bool = False,
    *,
    app: AgentApp | None = None,
) -> ThreadingHTTPServer:
    if not token:
        raise ValueError("Set EVOMESH_SERVER_TOKEN before starting the task API")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, status: int, body):
            payload = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            valid = hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()
            )
            if not valid:
                self.send_json(401, {"error": "Unauthorized"})
            return valid

        def route(self):
            url = urlparse(self.path)
            return [unquote(part) for part in url.path.strip("/").split("/")], parse_qs(url.query)

        def do_GET(self):
            if self.path == "/health":
                self.send_json(200, {"status": "ok"})
                return
            if not self.authorized():
                return
            parts, query = self.route()
            if app is None:
                self.send_json(404, {"error": "Not found"})
                return
            state = app.state
            try:
                if parts == ["tasks"]:
                    result = [public_task(r) for r in state.list_tasks()]
                elif len(parts) in (2, 3) and parts[0] == "tasks":
                    task = state.task(parts[1])
                    if task is None:
                        self.send_json(404, {"error": "Task not found"})
                        return
                    if len(parts) == 3 and parts[2] == "events":
                        result = state.task_events(parts[1])
                    elif len(parts) == 2:
                        result = public_task(task)
                    else:
                        self.send_json(404, {"error": "Not found"})
                        return
                elif parts == ["sessions"]:
                    result = state.list_sessions(query.get("query", [""])[0])
                elif len(parts) == 2 and parts[0] == "sessions":
                    result = state.session(parts[1])
                elif parts == ["memory"]:
                    result = state.recall(query.get("query", [""])[0])
                elif parts == ["schedules"]:
                    result = state.list_schedules()
                else:
                    self.send_json(404, {"error": "Not found"})
                    return
                self.send_json(200, result)
            except ValueError:
                self.send_json(400, {"error": "Invalid request"})

        def read_body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 64000 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid body size")
            self.connection.settimeout(15)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            return data

        def do_POST(self):
            if not self.authorized():
                return
            try:
                data = self.read_body()
                parts, _ = self.route()
                if parts == ["run"]:
                    task = data.get("task")
                    if not isinstance(task, str) or not task.strip():
                        raise ValueError("Expected task")
                    if app is not None:
                        session = data.get("session") or str(uuid.uuid4())
                        answer = app.run(task, session=session)
                        self.send_json(
                            200,
                            {"answer": answer, "session": session, "instance": app.config.instance},
                        )
                    else:
                        self.legacy_run(task)
                    return
                if app is None:
                    self.send_json(404, {"error": "Not found"})
                    return
                state = app.state
                if parts == ["tasks"]:
                    identifier = app.submit(
                        data["task"], session=data.get("session"), key=data.get("key")
                    )
                    self.send_json(202, {"id": identifier})
                    return
                if len(parts) == 3 and parts[0] == "tasks" and parts[2] in {"cancel", "retry"}:
                    action = state.cancel_task if parts[2] == "cancel" else state.retry_task
                    result = {"changed": action(parts[1])}
                elif parts == ["memory"]:
                    result = {"key": state.remember(data["key"], data["content"])}
                elif parts == ["memory", "delete"]:
                    result = {"deleted": state.forget(data["key"])}
                elif len(parts) == 3 and parts[0] == "sessions" and parts[2] == "delete":
                    result = {"deleted": state.forget_session(parts[1])}
                elif parts == ["schedules"]:
                    result = {"id": state.schedule(data["task"], data["interval_seconds"])}
                elif len(parts) == 3 and parts[0] == "schedules" and parts[2] == "disable":
                    result = {"changed": state.disable_schedule(parts[1])}
                else:
                    self.send_json(404, {"error": "Not found"})
                    return
                self.send_json(200, result)
            except SessionBusy:
                self.send_json(409, {"error": "Session already running"})
            except (ValueError, KeyError, TypeError, AttributeError):
                self.send_json(400, {"error": "Invalid JSON request or parameters; limit 64 KB"})
            except Exception:
                self.send_json(500, {"error": "Operation failed; inspect observations"})

        def legacy_run(self, task):
            agent, status, body = None, 200, {}
            try:
                agent = factory()
                body = {"answer": agent.run(task), "instance": agent.instance}
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
