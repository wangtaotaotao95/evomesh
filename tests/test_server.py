import threading

import httpx
import pytest

from evomesh import Agent
from evomesh.models import DemoModel
from evomesh.server import create_server
from evomesh.tools import workspace_tools


def test_task_api_auth_and_validation(store, project, tmp_path):
    def factory():
        return Agent(DemoModel(), store, workspace_tools(tmp_path), project=project)

    server = create_server("127.0.0.1", 0, "test-token", factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=url, trust_env=False) as client:
            assert client.get("/health").status_code == 200
            assert client.post("/run", json={"task": "hi"}).status_code == 401
            headers = {"Authorization": "Bearer test-token"}
            assert client.post("/run", headers=headers, json={}).status_code == 400
            assert client.post("/run", headers=headers, json=[]).status_code == 400
            result = client.post("/run", headers=headers, json={"task": "List files"})
            assert result.status_code == 200
            assert "Workspace files" in result.json()["answer"]
        with pytest.raises(ValueError, match="TOKEN"):
            create_server("127.0.0.1", 0, "", factory)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_failed_api_task_can_still_generate_evolution_proposal():
    class FailingAgent:
        last_observations = []

        def run(self, task):
            self.last_observations = ["observation-1"]
            raise RuntimeError("Step limit")

        def reflect(self):
            return ["proposal-1"]

    server = create_server("127.0.0.1", 0, "test-token", FailingAgent, auto_reflect=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                f"http://127.0.0.1:{server.server_port}/run",
                headers={"Authorization": "Bearer test-token"},
                json={"task": "fails"},
            )
        assert response.status_code == 500
        assert response.json()["proposals"] == ["proposal-1"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_durable_api_task_roundtrip_and_private_routes(store, project, tmp_path):
    from evomesh import AgentApp, Config

    app = AgentApp(
        Config(project=project, workspace=tmp_path / "workspace", repo=tmp_path, demo=True),
        store=store,
        initialize=False,
    )
    server = create_server("127.0.0.1", 0, "secret", app.agent, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
        ) as client:
            for path in ("/tasks", "/sessions", "/memory", "/schedules"):
                assert client.get(path).status_code == 401
            client.headers["Authorization"] = "Bearer secret"
            first = client.post("/run", json={"task": "List files", "session": "main"})
            assert first.status_code == 200
            assert first.json()["session"] == "main"
            assert len(client.get("/sessions/main").json()) == 4
            response = client.post("/tasks", json={"task": "List files", "key": "one"})
            assert response.status_code == 202
            identifier = response.json()["id"]
            assert app.work_once()["status"] == "succeeded"
            result = client.get(f"/tasks/{identifier}").json()
            assert result["status"] == "succeeded"
            assert "token" not in result
            assert client.get(f"/tasks/{identifier}/events").json()
            assert (
                client.post("/memory", json={"key": "tone", "content": "Concise"}).status_code
                == 200
            )
            assert client.get("/memory").json()[0]["content"] == "Concise"
            schedule = client.post(
                "/schedules", json={"task": "Check files", "interval_seconds": 60}
            )
            assert schedule.status_code == 200
            assert client.post(f"/schedules/{schedule.json()['id']}/disable", json={}).json()[
                "changed"
            ]
            assert client.post("/tasks", json={"task": []}).status_code == 400
            assert client.get("/tasks/missing").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_console_assets_and_embedded_worker(store, project, tmp_path):
    import time

    from evomesh import AgentApp, Config

    app = AgentApp(
        Config(project=project, workspace=tmp_path, demo=True), store=store, initialize=False
    )
    server = create_server("127.0.0.1", 0, "console-token", app.agent, app=app, with_worker=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as c:
            for path in ("/", "/app.js", "/style.css"):
                response = c.get(path)
                assert response.status_code == 200
                assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            for path in ("/status", "/channels", "/deliveries", "/evolution"):
                assert c.get(path).status_code == 401
            c.headers["Authorization"] = "Bearer console-token"
            status = c.get("/status").json()
            assert status["embedded_worker"] is True and "database_url" not in status
            task = c.post("/tasks", json={"task": "List files", "session": "console"}).json()["id"]
            deadline = time.time() + 10
            while time.time() < deadline:
                result = c.get("/tasks/" + task).json()
                if result["status"] == "succeeded":
                    break
                time.sleep(0.05)
            assert result["status"] == "succeeded"
            assert c.get("/status").json()["tasks"]["succeeded"] == 1
            assert "Workspace files" in c.get("/sessions/console").json()[-1]["content"]
            assert c.post("/observations", json={"detail": "Improve navigation"}).status_code == 200
            assert c.get("/evolution").json()["observations"][0]["detail"] == "Improve navigation"
            assert c.post("/deliveries/id/retry", json={}).status_code == 400
            assert c.post("/webhooks/missing", json={}).status_code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert not server.worker.is_alive()


def test_webhook_http_ack_dedup_and_worker_reply(store, project, tmp_path, monkeypatch):
    import json
    import time

    from evomesh import AgentApp, Config
    from evomesh.channels import ChannelHub

    monkeypatch.setenv("TEST_BOT", "fake-bot-token")
    monkeypatch.setenv("TEST_HOOK", "test-hook-secret")
    app = AgentApp(
        Config(
            project=project,
            workspace=tmp_path,
            demo=True,
            channels={
                "telegram": {
                    "kind": "telegram",
                    "bot_token_env": "TEST_BOT",
                    "webhook_secret_env": "TEST_HOOK",
                    "allowed_senders": ["42"],
                }
            },
        ),
        store=store,
        initialize=False,
    )
    sent = []

    def reply(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    provider = httpx.Client(transport=httpx.MockTransport(reply))
    hub = ChannelHub(app, client=provider)
    server = create_server(
        "127.0.0.1", 0, "console-token", app.agent, app=app, channels=hub, with_worker=True
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        event = {
            "update_id": 1,
            "message": {
                "message_id": 3,
                "from": {"id": 42, "is_bot": False},
                "chat": {"id": 42, "type": "private"},
                "text": "List files",
            },
        }
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as c:
            assert c.post("/webhooks/telegram", json=event).status_code == 403
            headers = {"X-Telegram-Bot-Api-Secret-Token": "test-hook-secret"}
            for _ in range(3):
                assert c.post("/webhooks/telegram", headers=headers, json=event).status_code == 200
            deadline = time.time() + 10
            while time.time() < deadline and not sent:
                time.sleep(0.05)
            assert len(sent) == 1
            assert "Workspace files" in json.loads(sent[0].content)["text"]
            assert len(app.state.list_tasks()) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        provider.close()


def test_cognition_api_and_source_catalog(store, project, tmp_path):
    from evomesh import AgentApp, Config

    app = AgentApp(
        Config(project=project, repo=tmp_path, workspace=tmp_path / "workspace", demo=True),
        store=store,
        initialize=False,
    )
    (tmp_path / "workspace" / "notes.md").write_text("Knowledge for whole-stack evolution\n")
    server = create_server("127.0.0.1", 0, "secret", app.agent, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as c:
            for path in ("/knowledge", "/plans", "/skills", "/capabilities", "/runs"):
                assert c.get(path).status_code == 401
            c.headers["Authorization"] = "Bearer secret"
            assert c.post("/knowledge/index", json={"path": "notes.md"}).status_code == 200
            hit = c.get("/knowledge?query=evolution").json()[0]
            assert hit["path"] == "notes.md" and hit["start_line"] == 1
            plan = {
                "session": "main",
                "title": "Research",
                "steps": [{"title": "Read", "status": "todo"}],
                "version": 0,
            }
            assert c.post("/plans", json=plan).json()["version"] == 1
            assert c.post("/plans", json=plan).status_code == 400
            token, _ = app.state.acquire_session("main")
            assert c.post("/plans", json={**plan, "version": 1}).status_code == 409
            app.state.release_session("main", token)
            assert c.get("/plans/main").json()["title"] == "Research"
            assert c.get("/capabilities").json()
            assert c.post("/run", json={"task": "List files", "session": "main"}).status_code == 200
            assert c.get("/runs").json()[0]["metrics"]["model_calls"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
