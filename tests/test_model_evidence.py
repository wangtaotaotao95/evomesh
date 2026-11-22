import json

import httpx
import pytest

from evomesh import Agent, AgentApp, Config
from evomesh.agent import RunCancelled
from evomesh.context import ContextBudgetExceeded
from evomesh.evolution import git
from evomesh.models import ChatModel


@pytest.mark.parametrize("preceding", ["none", "tool_error", "tool_success"])
@pytest.mark.parametrize("failure", ["http", "timeout", "invalid_json"])
def test_model_failures_keep_distinct_private_evidence_and_metrics(
    store, project, tmp_path, monkeypatch, preceding, failure
):
    requests = []

    def post(url, **kwargs):
        requests.append(kwargs)
        if preceding != "none" and len(requests) == 1:
            name, arguments = (
                ("read_file", {"path": "PRIVATE_MISSING.txt"})
                if preceding == "tool_error"
                else (
                    "write_file",
                    {
                        "path": "PRIVATE_ARTIFACT.txt",
                        "content": "PRIVATE_RESULT",
                        "create_only": True,
                    },
                )
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "fixture",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(arguments),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE_TRANSPORT_DETAIL")
        if failure == "http":
            return httpx.Response(503, text="PRIVATE_PROVIDER_BODY")
        return httpx.Response(200, text="PRIVATE_INVALID_JSON")

    monkeypatch.setattr(httpx, "post", post)
    expected = {
        "http": RuntimeError,
        "timeout": httpx.ReadTimeout,
        "invalid_json": json.JSONDecodeError,
    }[failure]
    model = ChatModel("synthetic", "https://PRIVATE_ENDPOINT.invalid/v1", "PRIVATE_KEY")
    with AgentApp(Config(project=project, workspace=tmp_path), store=store, model=model) as app:
        with pytest.raises(expected):
            app.run("PRIVATE_TASK", session="fixture")
        metrics = app.last_run_metrics
        attempts = 1 if preceding == "none" else 2
        assert len(requests) == attempts == metrics["model_calls"]
        assert metrics["model_errors"] == 1 and metrics["status"] == "failed"
        assert metrics["tool_calls"] == int(preceding != "none")
        assert metrics["tool_errors"] == int(preceding == "tool_error")
        assert app.state.session("fixture") == []
        assert app.learning.list()[0]["metrics"] == metrics
    if preceding == "tool_success":
        # The earlier action survives; inference failure must not trigger replay.
        assert (tmp_path / "PRIVATE_ARTIFACT.txt").read_text() == "PRIVATE_RESULT"
    rows = store.observations(project)
    errors = [row for row in rows if row["kind"] == "model_error"]
    assert len(errors) == 1 and len(rows) == 1 + int(preceding == "tool_error")
    assert not any(row["kind"] == "run_error" for row in rows)
    detail = json.loads(errors[0]["detail"])
    assert detail == {
        "capability": "models",
        "source_paths": ["src/evomesh/models.py"],
        "error": expected.__name__,
        "step": attempts - 1,
    }
    assert set(metrics["observation_ids"]) == {row["id"] for row in rows}
    assert "PRIVATE_" not in json.dumps(rows) and "PRIVATE_" not in json.dumps(metrics)


def test_model_control_flow_cancellation_is_not_a_model_error(store, project, tmp_path):
    class CancelledModel:
        def complete(self, messages, tools):
            raise RunCancelled("Synthetic cancellation")

    with AgentApp(
        Config(project=project, workspace=tmp_path), store=store, model=CancelledModel()
    ) as app:
        with pytest.raises(RunCancelled):
            app.run("Synthetic cancellation")
        assert app.last_run_metrics["status"] == "cancelled"
        assert app.last_run_metrics["model_calls"] == 1
        assert app.last_run_metrics["model_errors"] == 0
    assert store.observations(project) == []


def test_context_failure_before_inference_stays_a_context_observation(store, project):
    class UnusedModel:
        def complete(self, messages, tools):
            pytest.fail("An over-budget request must not reach the model")

    agent = Agent(UnusedModel(), store, [], project=project, context_chars=1)
    with pytest.raises(ContextBudgetExceeded):
        agent.run("Synthetic task")
    assert agent.metrics["model_calls"] == agent.metrics["model_errors"] == 0
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "context_limit"
    assert json.loads(rows[0]["detail"])["capability"] == "context"


def test_proactive_reflection_model_failure_points_to_model_sources(store, project, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "SOUL.md").write_text("Synthetic assistant\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Synthetic source")

    class FailedReviewer:
        def complete(self, messages, tools):
            raise TimeoutError("PRIVATE_REVIEW_FAILURE")

    with pytest.raises(TimeoutError):
        Agent(FailedReviewer(), store, [], project=project, repo=repo).reflect(proactive=True)
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "model_error"
    assert json.loads(rows[0]["detail"])["source_paths"] == ["src/evomesh/models.py"]
    assert "PRIVATE_REVIEW_FAILURE" not in json.dumps(rows)
    assert not store.list(project)
