from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import ValidationError
from sqlalchemy import update

from evomesh import AgentApp, Config, Team, tool
from evomesh.models import DemoModel
from evomesh.runtime_store import RuntimeStore, SessionBusy, sessions, tasks
from evomesh.store import LeaseLost


class EchoHistory:
    def __init__(self):
        self.received = []

    def complete(self, messages, tools):
        self.received.append(messages)
        return {
            "role": "assistant",
            "content": " | ".join(m["content"] for m in messages if m["role"] == "user"),
        }


def app_for(store, project, tmp_path, **kwargs):
    config = Config(
        project=project, workspace=tmp_path / "workspace", repo=tmp_path, demo=True, **kwargs
    )
    return AgentApp(config, store=store, model=EchoHistory(), initialize=False)


def test_session_resumes_across_instances_and_retains_whole_turns(store, project, tmp_path):
    first = app_for(store, project, tmp_path, history_turns=2, instance="a")
    first.run("one", session="main")
    second = app_for(store, project, tmp_path, history_turns=2, instance="b")
    assert second.run("two", session="main") == "one | two"
    assert second.run("three", session="main") == "one | two | three"
    history = second.state.session("main")
    assert [m["content"] for m in history if m["role"] == "user"] == ["two", "three"]
    assert second.state.list_sessions("three")[0]["name"] == "main"
    assert second.state.forget_session("main")
    assert not second.state.session("main")


def test_owner_isolation_memory_injection_and_deletion(store, project, tmp_path):
    owner = app_for(store, project, tmp_path)
    owner.state.remember("style", "Use concise answers")
    owner.state.remember("style", "Use short answers")
    owner.run("hello", session="main")
    assert "Use short answers" in owner.model_factory().received[-1][0]["content"]
    another = app_for(store, project, tmp_path, owner="other")
    assert not another.state.recall()
    assert not another.state.session("main")
    assert not another.state.forget("style")
    assert len(owner.state.recall("short")) == 1
    assert owner.state.forget("style")
    assert not owner.state.recall()


def test_session_exclusion_and_stale_owner_fencing(store, project):
    state = RuntimeStore(store, project)
    token, _ = state.acquire_session("main")
    with pytest.raises(SessionBusy):
        state.acquire_session("main")
    assert not state.forget_session("main")
    with store.engine.begin() as conn:
        conn.execute(update(sessions).where(sessions.c.project == project).values(lease_until=0))
    new, _ = state.acquire_session("main")
    with pytest.raises(LeaseLost):
        state.release_session("main", token, [])
    state.release_session("main", new, [{"role": "user", "content": "new"}])
    assert state.session("main")[0]["content"] == "new"


def test_failed_run_preserves_last_complete_conversation(store, project, tmp_path):
    app = app_for(store, project, tmp_path)
    app.run("good", session="main")
    before = app.state.session("main")

    class Fail:
        def complete(self, *_):
            raise RuntimeError("inference failed")

    app.model_factory = Fail
    with pytest.raises(RuntimeError):
        app.run("bad", session="main")
    assert app.state.session("main") == before
    token, _ = app.state.acquire_session("main")
    app.state.release_session("main", token)


def test_task_deduplication_and_owner_isolation(store, project):
    state = RuntimeStore(store, project)
    identifier = state.submit("task", key="key")
    assert state.submit("task", key="key") == identifier
    with pytest.raises(ValueError, match="Idempotency"):
        state.submit("different", key="key")
    other = RuntimeStore(store, project, "other")
    assert other.task(identifier) is None
    assert not other.cancel_task(identifier)
    assert other.claim_task() is None


def test_multiple_workers_execute_distinct_tasks(store, project, tmp_path):
    a, b = app_for(store, project, tmp_path), app_for(store, project, tmp_path)
    ids = [a.submit(f"task-{i}") for i in range(6)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda i: (a if i % 2 else b).work_once(), range(6)))
    assert len({r["id"] for r in results}) == 6
    assert {a.state.task(i)["status"] for i in ids} == {"succeeded"}
    assert all(a.state.task_events(i)[0]["kind"] == "run_started" for i in ids)
    assert all(a.state.task(i)["attempts"] == 1 for i in ids)


def test_expired_task_needs_explicit_retry(store, project):
    state = RuntimeStore(store, project)
    identifier = state.submit("write a file")
    old = state.claim_task()
    with store.engine.begin() as conn:
        conn.execute(update(tasks).where(tasks.c.id == identifier).values(lease_until=0))
    assert state.claim_task() is None
    assert state.task(identifier)["status"] == "interrupted"
    with pytest.raises(LeaseLost):
        state.finish_task(identifier, old["token"], "succeeded", "stale result")
    assert state.retry_task(identifier)
    new = state.claim_task()
    assert new["token"] != old["token"]
    assert new["attempts"] == 2


def test_cancel_before_and_during_execution(store, project, tmp_path):
    app = app_for(store, project, tmp_path)
    identifier = app.submit("do work")
    assert app.state.cancel_task(identifier)
    assert app.work_once() is None
    assert app.state.retry_task(identifier)
    app.on_event = lambda event: app.state.cancel_task(identifier)
    result = app.work_once()
    assert result["status"] == "cancelled"
    assert not app.state.session(result["session"])


def test_schedules_are_atomic_across_instances_and_coalesce_missed_runs(store, project):
    a, b = RuntimeStore(store, project), RuntimeStore(store, project)
    identifier = a.schedule("daily check", 60, start_at=100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda state: state.tick(1000), [a, b]))
    assert sum(len(r) for r in results) == 1
    assert len(a.list_tasks()) == 1
    assert a.list_schedules()[0]["next_run"] == 1060
    assert a.tick(1059) == []
    assert len(a.tick(1060)) == 1
    assert a.disable_schedule(identifier)
    assert not a.tick(2000)


def test_schedule_enqueue_failure_rolls_back_next_run(store, project, monkeypatch):
    state = RuntimeStore(store, project)
    state.schedule("check", 60, start_at=100)

    def fail(*_):
        raise RuntimeError("serialization failure")

    monkeypatch.setattr(state, "_task_values", fail)
    with pytest.raises(RuntimeError):
        state.tick(100)
    assert state.list_schedules()[0]["next_run"] == 100


def test_task_session_busy_is_requeued_without_execution(store, project, tmp_path):
    app = app_for(store, project, tmp_path)
    token, _ = app.state.acquire_session("main")
    identifier = app.submit("pending", session="main")
    assert app.work_once()["status"] == "queued"
    app.state.release_session("main", token)
    assert app.work_once()["status"] == "succeeded"
    assert app.state.task(identifier)["result"] == "pending"


def test_tool_decorator_defaults_validation_and_team(store, project, tmp_path):
    @tool
    def greet(name: str, excited: bool = False) -> str:
        """Say hello."""
        return f"Hello {name}" + ("!" if excited else "")

    assert greet.invoke('{"name":"Ada"}') == "Hello Ada"
    with pytest.raises(ValidationError):
        greet.invoke('{"name":1}')
    a, b = app_for(store, project, tmp_path), app_for(store, project + "-b", tmp_path)
    a.register(greet)
    with pytest.raises(ValueError, match="Duplicate"):
        a.register(greet)
    team = Team({"writer": a, "reviewer": b}, max_workers=2)
    assert team.run({"writer": "draft", "reviewer": "review"}) == {
        "writer": "draft",
        "reviewer": "review",
    }
    assert team.as_tool().invoke('{"agent":"writer","task":"delegated"}') == "delegated"
    with pytest.raises(ValueError):
        team.run({"unknown": "x"})


def test_tool_policy_prevents_execution(store, project, tmp_path):
    app = app_for(store, project, tmp_path)
    app.model_factory = DemoModel
    app.before_tool = lambda name, args: False
    answer = app.run("List files")
    assert "PermissionError" in answer
    assert app.store.observations(project)[0]["kind"] == "tool_error"


def test_async_bridge(store, project, tmp_path):
    import asyncio

    app = app_for(store, project, tmp_path)
    assert asyncio.run(app.arun("hello", session="async")) == "hello"
    assert app.state.session("async")
