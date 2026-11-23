import json
import threading

import pytest

from evomesh import Agent, AgentApp, Config, Tool
from evomesh.agent import RunCancelled
from evomesh.tools import object_schema


def calls(*names):
    return {
        "role": "assistant",
        "tool_calls": [
            {"id": str(i), "type": "function", "function": {"name": name, "arguments": "{}"}}
            for i, name in enumerate(names)
        ],
    }


@pytest.mark.parametrize("source", ["handler", "policy"])
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("earlier_error", [False, True])
def test_tool_cancellation_stops_batch_without_reflection_or_replay(
    store, project, tmp_path, monkeypatch, source, queued, earlier_error
):
    effects, reviews, events = [], [], []

    def save():
        effects.append("saved")
        (tmp_path / "artifact.txt").write_text("Synthetic prior effect")

    def fail():
        raise ValueError("PRIVATE_TOOL_DIAGNOSTIC")

    def stop():
        effects.append("cancel-handler")
        raise RunCancelled("PRIVATE_CANCEL_REASON")

    def policy(name, arguments):
        if source == "policy" and name == "stop":
            raise RunCancelled("PRIVATE_POLICY_REASON")
        return True

    class Model:
        attempts = 0

        def complete(self, messages, tools):
            self.attempts += 1
            if self.attempts > 1:
                return {"role": "assistant", "content": "Unexpected continuation"}
            return calls("save", *(["fail"] if earlier_error else []), "stop", "later")

    monkeypatch.setattr(Agent, "reflect", lambda self, **kw: reviews.append(True) or [])
    model = Model()
    registry = [
        Tool(name, "Synthetic tool", object_schema({}), handler)
        for name, handler in (
            ("save", save),
            ("fail", fail),
            ("stop", stop),
            ("later", lambda: effects.append("later")),
        )
    ]
    with AgentApp(
        Config(project=project, workspace=tmp_path, auto_reflect=True),
        store=store,
        model=model,
        tools=registry,
        before_tool=policy,
        on_event=events.append,
    ) as app:
        if queued:
            identifier = app.submit("Synthetic task", session="fixture")
            result = app.work_once()
            assert result["id"] == identifier and result["status"] == "cancelled"
            assert result["error"] is None and result["attempts"] == 1
            assert app.work_once() is None
        else:
            with pytest.raises(RunCancelled, match="PRIVATE_"):
                app.run("Synthetic task", session="fixture")
        metrics = app.last_run_metrics
        assert metrics["status"] == "cancelled"
        assert metrics["tool_calls"] == 2 + int(earlier_error)
        assert metrics["tool_errors"] == int(earlier_error)
        assert metrics["model_errors"] == 0 and metrics["model_calls"] == 1
        assert app.learning.list()[0]["metrics"] == metrics
        assert app.state.session("fixture") == []
        # The session lease was released, so later user work can acquire it.
        token, history = app.state.acquire_session("fixture")
        app.state.release_session("fixture", token, history)
    assert model.attempts == 1 and not reviews
    assert effects == ["saved"] + (["cancel-handler"] if source == "handler" else [])
    assert (tmp_path / "artifact.txt").read_text() == "Synthetic prior effect"
    assert events[-1]["kind"] == "run_cancelled"
    assert not any(event["kind"] == "run_failed" for event in events)
    assert not any(
        event["kind"] == "tool_completed" and event["tool"] == "stop" for event in events
    )
    rows = store.observations(project)
    assert len(rows) == int(earlier_error)
    assert all(row["kind"] == "tool_error" for row in rows)
    assert set(metrics["observation_ids"]) == {row["id"] for row in rows}
    assert "PRIVATE_" not in json.dumps(rows) + json.dumps(metrics) + json.dumps(events)


@pytest.mark.parametrize("source", ["handler", "event"])
def test_final_step_cancellation_wins_over_step_limit(store, project, source):
    cancelled = threading.Event()
    events = []

    def effect():
        if source == "handler":
            cancelled.set()
        return "Synthetic completed effect"

    def emit(event):
        events.append(event)
        if source == "event" and event["kind"] == "tool_completed":
            cancelled.set()

    class Model:
        def complete(self, messages, tools):
            return calls("effect")

    agent = Agent(
        Model(),
        store,
        [Tool("effect", "Synthetic tool", object_schema({}), effect)],
        project=project,
        max_steps=1,
        cancelled=cancelled.is_set,
        on_event=emit,
    )
    prior = [{"role": "user", "content": "Earlier"}, {"role": "assistant", "content": "Done"}]
    agent.history = list(prior)
    with pytest.raises(RunCancelled):
        agent.run("Synthetic final-step cancellation")
    assert agent.history == prior
    assert agent.metrics["status"] == "cancelled" and agent.metrics["tool_errors"] == 0
    assert events[-2] == {"kind": "tool_completed", "tool": "effect", "outcome": "ok"}
    assert events[-1]["kind"] == "run_cancelled"
    assert not store.observations(project)


def test_task_cancelled_in_final_tool_keeps_cancelled_metrics(store, project, tmp_path):
    class Model:
        def complete(self, messages, tools):
            return calls("cancel_task")

    with AgentApp(
        Config(project=project, workspace=tmp_path, max_steps=1), store=store, model=Model()
    ) as app:
        identifier = app.submit("Synthetic task", session="fixture")
        app.register(
            Tool(
                "cancel_task",
                "Synthetic cancellation",
                object_schema({}),
                lambda: app.state.cancel_task(identifier),
            )
        )
        assert app.work_once()["status"] == "cancelled"
        assert app.last_run_metrics["status"] == "cancelled"
        assert not app.state.session("fixture")
        assert not store.observations(project)
