import copy
import json

import httpx
import pytest

from evomesh import Agent, AgentApp, Config
from evomesh.evolution import git
from evomesh.models import ChatModel, InvalidModelResponse


def call(identifier="write", name="write_file", arguments=None):
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"path": "artifact.txt", "content": "PRIVATE_RESULT"})
            if arguments is None
            else arguments,
        },
    }


def model_for(message, adapter, monkeypatch):
    if adapter == "http":

        def post(*args, **kwargs):
            return httpx.Response(200, json={"choices": [{"message": message}]})

        monkeypatch.setattr(httpx, "post", post)
        return ChatModel("fixture", "https://example.invalid/v1")

    class CustomModel:
        def complete(self, messages, tools):
            return copy.deepcopy(message)

    return CustomModel()


@pytest.mark.parametrize("adapter", ["custom", "http"])
@pytest.mark.parametrize(
    "broken",
    [
        None,
        "PRIVATE_CALL",
        {"id": "bad", "type": "PRIVATE_TYPE", "function": {}},
        {"type": "function", "function": {}},
        {**call("bad"), "id": []},
        {**call("bad"), "id": ""},
        call("write"),  # Duplicate IDs make subsequent receipts ambiguous.
        {**call("bad"), "function": None},
        {**call("bad"), "function": {"arguments": "{}"}},
        {**call("bad"), "function": {"name": [], "arguments": "{}"}},
        {**call("bad"), "function": {"name": "", "arguments": "{}"}},
        {**call("bad"), "function": {"name": "write_file", "arguments": {}}},
    ],
)
def test_validate_whole_batch_before_any_side_effect(
    store, project, tmp_path, monkeypatch, adapter, broken
):
    message = {"role": "assistant", "tool_calls": [call(), broken]}
    model = model_for(message, adapter, monkeypatch)
    events = []
    with AgentApp(Config(project=project, workspace=tmp_path), store=store, model=model) as app:
        with pytest.raises(InvalidModelResponse) as error:
            app.run("PRIVATE_TASK", session="fixture", on_event=events.append)
        metrics = app.last_run_metrics
        assert metrics["model_calls"] == metrics["model_errors"] == 1
        assert metrics["tool_calls"] == metrics["tool_errors"] == 0
        assert metrics["status"] == "failed"
        assert app.state.session("fixture") == []
        assert app.learning.list()[0]["metrics"] == metrics
    assert not (tmp_path / "artifact.txt").exists()
    assert not any(event["kind"] == "tool_started" for event in events)
    observations = store.observations(project)
    assert len(observations) == 1 and observations[0]["kind"] == "model_error"
    assert json.loads(observations[0]["detail"]) == {
        "capability": "models",
        "source_paths": ["src/evomesh/models.py"],
        "error": "InvalidModelResponse",
        "step": 0,
    }
    assert "PRIVATE_" not in str(error.value) + json.dumps(observations) + json.dumps(metrics)


@pytest.mark.parametrize("adapter", ["custom", "http"])
@pytest.mark.parametrize(
    "message",
    [
        [],
        {},
        {"role": "user", "content": "PRIVATE_BODY"},
        {"role": "assistant", "content": ["PRIVATE_BODY"]},
        {"role": "assistant", "reasoning_content": {"private": "PRIVATE_BODY"}},
        {"role": "assistant", "tool_calls": {}},
        {"role": "assistant", "tool_calls": ""},
    ],
)
def test_malformed_reply_fails_as_model_evidence(store, project, monkeypatch, adapter, message):
    agent = Agent(model_for(message, adapter, monkeypatch), store, [], project=project)
    with pytest.raises(InvalidModelResponse):
        agent.run("Synthetic task")
    assert agent.metrics["model_errors"] == 1 and agent.metrics["status"] == "failed"
    assert store.observations(project)[0]["kind"] == "model_error"
    assert not agent.history


def test_valid_batches_and_recoverable_tool_errors_keep_existing_protocol(store, project, tmp_path):
    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                receipts = messages[-3:]
                assert receipts[0]["tool_call_id"] == "write"
                assert "Saved" in receipts[0]["content"]
                assert json.loads(receipts[1]["content"])["error"] == "KeyError"
                assert json.loads(receipts[2]["content"])["error"] == "JSONDecodeError"
                assert messages[-4]["reasoning_content"] == "Synthetic continuation"
                return {"role": "assistant", "content": "Handled", "tool_calls": None}
            return {
                "role": "assistant",
                "reasoning_content": "Synthetic continuation",
                "tool_calls": [
                    {key: value for key, value in call().items() if key != "type"},
                    call("unknown", "unknown"),
                    call("bad-json", arguments="{"),
                ],
            }

    with AgentApp(Config(project=project, workspace=tmp_path), store=store, model=Model()) as app:
        assert app.run("Synthetic task") == "Handled"
        assert app.last_run_metrics["model_errors"] == 0
        assert app.last_run_metrics["tool_calls"] == 3
        assert app.last_run_metrics["tool_errors"] == 2
    assert (tmp_path / "artifact.txt").read_text() == "PRIVATE_RESULT"


def test_reflection_validates_batch_before_saving_proposals(store, project, tmp_path):
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "SOUL.md").write_text("Synthetic soul")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Synthetic source")
    store.observe(project, "fixture", "feedback", "Improve synthetic soul")

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] != "tool":
                return {
                    "role": "assistant",
                    "tool_calls": [call("read", "read_source", '{"path":"SOUL.md"}')],
                }
            return {
                "role": "assistant",
                "tool_calls": [
                    call(
                        "proposal",
                        "propose_evolution",
                        json.dumps(
                            {
                                "target": "soul",
                                "rationale": "Synthetic improvement",
                                "files": [{"path": "SOUL.md", "content": "Updated synthetic soul"}],
                            }
                        ),
                    ),
                    {"type": "function"},
                ],
            }

    agent = Agent(Model(), store, [], project=project, repo=tmp_path)
    with pytest.raises(InvalidModelResponse):
        agent.reflect()
    assert not store.list(project)
    assert any(row["kind"] == "model_error" for row in store.observations(project))
    assert (tmp_path / "SOUL.md").read_text() == "Synthetic soul"
