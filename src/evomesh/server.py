"""Authenticated API shared by personal assistants and embedded agent applications."""

from __future__ import annotations

import hmac
import json
import threading
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import func, select

from .agent import Agent
from .app import AgentApp
from .channels import ChannelDenied, ChannelHub
from .runtime_store import SessionBusy, tasks


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
    channels: ChannelHub | None = None,
    with_worker: bool = False,
) -> ThreadingHTTPServer:
    if not token:
        raise ValueError("Set EVOMESH_SERVER_TOKEN before starting the task API")

    hub = channels or (ChannelHub(app) if app else None)

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

        def send_asset(self, filename, mime):
            payload = files("evomesh").joinpath("web", filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                    "base-uri 'none'; form-action 'self'"
                ),
            )
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if app is not None and self.path in assets:
                self.send_asset(*assets[self.path])
                return
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
                if parts == ["status"]:
                    from . import __version__

                    with app.store.engine.connect() as conn:
                        counts = dict(
                            conn.execute(
                                select(tasks.c.status, func.count())
                                .where(state.scope(tasks))
                                .group_by(tasks.c.status)
                            ).all()
                        )
                    result = {
                        "version": __version__,
                        "project": app.config.project,
                        "owner": app.config.owner,
                        "instance": app.config.instance,
                        "database": app.store.engine.dialect.name,
                        "model": "DemoModel" if app.config.demo else app.config.model,
                        "demo": app.config.demo,
                        "tasks": counts,
                        "embedded_worker": bool(server.worker and server.worker.is_alive()),
                        "worker_error": server.worker_error,
                    }
                elif parts == ["capabilities"]:
                    result = app.capabilities()
                elif parts == ["runs"]:
                    result = app.learning.list()
                elif parts == ["skills"]:
                    result = app.skills.list()
                elif len(parts) == 2 and parts[0] == "skills":
                    result = app.skills.load(parts[1])
                elif parts == ["plans"]:
                    result = app.cognition.list_plans()
                elif len(parts) == 2 and parts[0] == "plans":
                    result = app.cognition.plan(parts[1])
                elif parts == ["knowledge"]:
                    result = (
                        app.cognition.search(query["query"][0])
                        if query.get("query")
                        else app.cognition.list_documents()
                    )
                elif parts == ["channels"]:
                    result = hub.status()
                elif parts == ["deliveries"]:
                    result = hub.state.list()
                elif parts == ["evolution"]:
                    result = {
                        "observations": app.store.observations(app.config.project, 100),
                        "proposals": [public_task(r) for r in app.store.list(app.config.project)],
                    }
                elif parts == ["tasks"]:
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
            except Exception:
                self.send_json(503, {"error": "Storage temporarily unavailable"})

        def read_raw(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 64000 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid body size")
            self.connection.settimeout(15)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request")
            return body

        def read_body(self):
            data = json.loads(self.read_raw())
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            return data

        def do_POST(self):
            parts, _ = self.route()
            if len(parts) == 2 and parts[0] == "webhooks" and hub:
                try:
                    result = hub.receive(parts[1], dict(self.headers), self.read_raw())
                    self.send_json(200, result)
                except ChannelDenied:
                    self.send_json(403, {"error": "Channel authentication or policy rejected"})
                except (ValueError, KeyError, TypeError, AttributeError):
                    self.send_json(400, {"error": "Invalid channel payload"})
                except Exception:
                    self.send_json(503, {"error": "Channel temporarily unavailable"})
                return
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
                if len(parts) == 3 and parts[0] == "deliveries" and parts[2] == "retry":
                    if data.get("acknowledge_duplicate_risk") is not True:
                        raise ValueError("Explicit duplicate risk acknowledgement required")
                    result = {"changed": hub.state.retry_reply(parts[1])}
                elif parts == ["plans"]:
                    result = app.cognition.save_plan(
                        data["session"], data["title"], data["steps"], data["version"]
                    )
                elif parts == ["knowledge", "index"]:
                    result = app.cognition.index_document(data["path"])
                elif parts == ["knowledge", "remove"]:
                    result = {"deleted": app.cognition.remove_document(data["id"])}
                elif parts == ["observations"]:
                    detail = data["detail"]
                    if not isinstance(detail, str) or not 1 <= len(detail.strip()) <= 16000:
                        raise ValueError("Expected feedback")
                    result = {
                        "id": app.store.observe(
                            app.config.project, app.config.instance, "feedback", detail
                        )
                    }
                elif len(parts) == 3 and parts[0] == "tasks" and parts[2] in {"cancel", "retry"}:
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

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        worker = None
        worker_error = None

        def server_close(self):
            stop.set()
            if self.worker:
                self.worker.join()
            super().server_close()
            if hub and channels is None:
                hub.close()

    stop = threading.Event()
    server = Server((host, port), Handler)

    def work():
        while not stop.is_set():
            try:
                hub.deliver_once()
                app.work_once()
                hub.deliver_once()
                server.worker_error = None
            except Exception as exc:
                server.worker_error = type(exc).__name__
            stop.wait(0.5)

    if with_worker:
        if app is None:
            server.server_close()
            raise ValueError("Embedded worker requires AgentApp")
        server.worker = threading.Thread(target=work, daemon=True)
        server.worker.start()
    return server
