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
