import json

import pytest

from evomesh import Agent
from evomesh.evolution import git
from evomesh.models import ChatModel, DemoModel
from evomesh.tools import workspace_tools


class ScriptedModel:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.received = []

    def complete(self, messages, tools):
        self.received.append(list(messages))
        return next(self.messages)


def call(name, arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_offline_assistant_and_conversation(store, project, tmp_path):
    (tmp_path / "note.txt").write_text("hi")
    agent = Agent(DemoModel(), store, workspace_tools(tmp_path), project=project)
    assert "note.txt" in agent.run("List files")
    assert "note.txt" in agent.run("List again")
    assert len(agent.history) == 8


def test_tool_failure_records_friction_without_private_arguments(store, project, tmp_path):
    model = ScriptedModel(
        [
            call("read_file", {"path": "private-missing-file"}),
            {"role": "assistant", "content": "File is missing"},
        ]
    )
    agent = Agent(model, store, workspace_tools(tmp_path), project=project)
    assert agent.run("Read my file") == "File is missing"
    row = store.observations(project)[0]
    assert row["kind"] == "tool_error"
    assert "private-missing-file" not in row["detail"]
    assert model.received[1][-1]["role"] == "tool"


def test_step_limit_and_path_boundary(store, project, tmp_path):
    model = ScriptedModel([call("list_files", {"path": "."})])
    agent = Agent(model, store, workspace_tools(tmp_path), max_steps=1, project=project)
    with pytest.raises(RuntimeError, match="step limit"):
        agent.run("loop")
    assert not agent.history
    assert store.observations(project)[0]["kind"] == "step_limit"
    tools = {t.name: t for t in workspace_tools(tmp_path)}
    with pytest.raises(ValueError):
        tools["write_file"].invoke(json.dumps({"path": "../escape", "content": "bad"}))
    with pytest.raises(ValueError):
        tools["read_file"].invoke('{"path":".env"}')


def test_reflection_generates_real_proposal(store, project, tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "SOUL.md").write_text("Be helpful\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    store.observe(project, "instance-b", "feedback", "Prefer concise answers")
    model = ScriptedModel(
        [
            call("read_source", {"path": "SOUL.md"}),
            call(
                "propose_evolution",
                {
                    "target": "soul",
                    "rationale": "Use concise answers",
                    "files": [{"path": "SOUL.md", "content": "Be concise\n"}],
                },
            ),
            {"role": "assistant", "content": "Submitted"},
        ]
    )
    agent = Agent(model, store, [], project=project, instance="instance-a", repo=repo)
    identifiers = agent.reflect()
    assert len(identifiers) == 1
    assert store.list(project)[0]["id"] == identifiers[0]
    assert (repo / "SOUL.md").read_text() == "Be helpful\n"


def test_compatible_http_adapter(monkeypatch):
    import httpx

    received = []

    def post(url, **kwargs):
        received.append((url, kwargs))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                            "refusal": None,
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    assert ChatModel("test-model", "https://example.invalid/v1", "key").complete(
        [{"role": "user", "content": "hi"}],
        [],
    ) == {"role": "assistant", "content": "ok"}
    assert received[0][0] == "https://example.invalid/v1/chat/completions"
    assert received[0][1]["headers"]["Authorization"] == "Bearer key"


def test_compatible_adapter_preserves_reasoning_for_tool_continuations(monkeypatch):
    import httpx

    requests = []
    assistant = call("list_files", {"path": "."})
    assistant["reasoning_content"] = "provider continuation state"

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        message = assistant if len(requests) == 1 else {"role": "assistant", "content": "Done"}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    monkeypatch.setattr(httpx, "post", post)
    model = ChatModel("deepseek-flash", "https://api.deepseek.com", "test-only")
    messages = [{"role": "user", "content": "List files"}]
    messages.append(model.complete(messages, []))
    messages.append({"role": "tool", "tool_call_id": "call-1", "content": "[]"})
    assert model.complete(messages, [])["content"] == "Done"
    assert requests[1]["messages"][1]["reasoning_content"] == "provider continuation state"
