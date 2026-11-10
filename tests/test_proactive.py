import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from evomesh import AgentApp, Config
from evomesh.evolution import git, revision, source_file
from evomesh.proactive import ProactiveEvolution, cycles
from evomesh.store import LeaseLost


def call(name, **arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "step",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class ReviewModel:
    def __init__(self):
        self.count = 0

    def complete(self, messages, tools):
        self.count += 1
        if self.count == 1:
            return call("read_source", path="tool.py")
        if self.count == 2:
            return call(
                "record_finding",
                detail="tool.answer returns 1; documented answer is 2",
                paths=["tool.py"],
            )
        if self.count == 3:
            identifier = (
                json.loads(messages[-1]["content"])
                if messages[-1]["content"].startswith('"')
                else messages[-1]["content"]
            )
            return call(
                "propose_evolution",
                target="tool",
                rationale="Return the documented answer",
                observation_ids=[identifier],
                files=[{"path": "tool.py", "content": "def answer():\n    return 2\n"}],
            )
        return {"role": "assistant", "content": "Submitted"}


def application(store, project, tmp_path, **options):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    git(repo, "init", "-b", "main")
    (repo / "tool.py").write_text('"""Return 2."""\ndef answer():\n    return 1\n')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return AgentApp(
        Config(project=project, repo=repo, workspace=tmp_path / "workspace", **options),
        store=store,
        model_factory=ReviewModel,
    )


def test_proactive_no_failures_records_finding_and_integrates(store, project, tmp_path):
    app = application(
        store,
        project,
        tmp_path,
        evolution_checks=[
            [sys.executable, "-c", "from tool import answer; assert answer() == 2"],
        ],
    )
    before = revision(app.config.repo)
    assert store.observations(project) == []
    result = app.evolve_once()
    assert result["status"] == "succeeded"
    assert len(result["proposals"]) == 1
    assert result["integration"]["status"] == "integrated"
    assert source_file(app.config.repo, result["integration"]["commit"], "tool.py").endswith(
        b"return 2\n"
    )
    assert revision(app.config.repo) == before
    evidence = store.observations(project)[0]
    assert evidence["kind"] == "source_review"
    assert json.loads(evidence["detail"])["base_commit"] == before
    assert app.evolve_once() is None
    assert "token" not in ProactiveEvolution(app).status()


def test_proactive_fences_replicas_and_recovers_expired_lease(store, project, tmp_path):
    app = application(store, project, tmp_path)
    workers = [ProactiveEvolution(app), ProactiveEvolution(app)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda w: w._claim(), workers))
    assert sum(t is not None for t in tokens) == 1
    previous = next(t for t in tokens if t)
    assert workers[0]._claim(force=True) is None
    with store.engine.begin() as conn:
        conn.execute(update(cycles).where(cycles.c.project == project).values(lease_until=0))
    assert workers[1]._claim() is not None
    with pytest.raises(LeaseLost):
        workers[0]._renew(previous)


def test_failed_review_cools_down_and_worker_runs_when_idle(store, project, tmp_path):
    app = application(store, project, tmp_path, proactive_evolution=True)
    app.model_factory = lambda: object()  # No complete() method; review must fail safely.
    assert app.work_once() is None
    status = ProactiveEvolution(app).status()
    assert status["status"] == "failed"
    assert status["report"] == {"error": "AttributeError"}
    assert app.evolve_once() is None


def test_unread_finding_is_rejected(store, project, tmp_path):
    app = application(store, project, tmp_path)

    class Unread:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                assert json.loads(messages[-1]["content"])["error"] == "ValueError"
                return {"role": "assistant", "content": "No finding"}
            return call("record_finding", detail="Unverified", paths=["tool.py"])

    app.model_factory = Unread
    assert app.evolve_once()["proposals"] == []
    assert not any(row["kind"] == "source_review" for row in store.observations(project))


def test_rejected_candidate_does_not_publish(store, project, tmp_path):
    app = application(
        store, project, tmp_path, evolution_checks=[[sys.executable, "-c", "raise SystemExit(1)"]]
    )
    before = revision(app.config.repo)
    result = app.evolve_once()
    assert result["integration"]["status"] == "rejected"
    assert revision(app.config.repo, "evolution/main") == before


def test_review_has_independent_step_budget_and_diagnostic(store, project, tmp_path):
    app = application(store, project, tmp_path, max_steps=1, evolution_max_steps=6)
    assert app.evolve_once()["status"] == "succeeded"
    assert app.config.max_steps == 1
    app.config.evolution_max_steps = 2
    result = app.evolve_once(force=True)
    assert result == {"status": "failed", "error": "StepLimitExceeded"}
    assert ProactiveEvolution(app).status()["report"] == {"error": "StepLimitExceeded"}


@pytest.mark.parametrize("budget", [0, 101, True, 1.5])
def test_review_step_budget_validation(budget):
    with pytest.raises(ValueError, match="evolution_max_steps"):
        Config(evolution_max_steps=budget).validate()


@pytest.mark.parametrize("failure", ["budget", "provider", "cancelled"])
def test_failed_review_retains_saved_proposals_and_skips_integration(
    store, project, tmp_path, monkeypatch, failure
):
    from evomesh.agent import RunCancelled

    app = application(
        store,
        project,
        tmp_path,
        evolution_max_steps=3 if failure == "budget" else 8,
        evolution_checks=[[sys.executable, "-c", "print('gate')"]],
    )

    class InterruptedReview(ReviewModel):
        def complete(self, messages, tools):
            if self.count == 3:
                error = RunCancelled if failure == "cancelled" else RuntimeError
                raise error("PRIVATE_PROVIDER_DETAIL")
            return super().complete(messages, tools)

    def must_not_integrate(*args):
        pytest.fail("Incomplete reviews must leave saved proposals pending")

    monkeypatch.setattr("evomesh.evolution.Integrator.once", must_not_integrate)
    app.model_factory = InterruptedReview
    result = app.evolve_once()
    expected = {
        "budget": "StepLimitExceeded",
        "provider": "RuntimeError",
        "cancelled": "RunCancelled",
    }[failure]
    assert result["status"] == "failed" and result["error"] == expected
    rows = store.list(project)
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    assert result["proposals"] == [rows[0]["id"]]
    report = ProactiveEvolution(app).status()["report"]
    assert report == {"error": expected, "proposals": result["proposals"]}
    assert "PRIVATE_PROVIDER_DETAIL" not in json.dumps(report)
    assert app.evolve_once() is None  # Cooldown still applies; no blind replay.


def test_integrator_error_keeps_successful_reflection_receipt(
    store, project, tmp_path, monkeypatch
):
    app = application(
        store, project, tmp_path, evolution_checks=[[sys.executable, "-c", "print('gate')"]]
    )

    def unavailable(*args):
        raise OSError("PRIVATE_RUNNER_DETAIL")

    monkeypatch.setattr("evomesh.evolution.Integrator.once", unavailable)
    result = app.evolve_once()
    identifier = store.list(project)[0]["id"]
    assert result == {"status": "failed", "proposals": [identifier], "error": "OSError"}
    assert ProactiveEvolution(app).status()["report"] == {
        "proposals": [identifier],
        "error": "OSError",
    }


def test_framework_reflection_exception_exposes_saved_receipt(store, project, tmp_path):
    from evomesh.agent import ReflectionFailed

    app = application(store, project, tmp_path, max_steps=3)
    agent = app.agent()
    with pytest.raises(ReflectionFailed) as raised:
        agent.reflect(proactive=True)
    assert raised.value.error_type == "StepLimitExceeded"
    assert raised.value.proposal_ids == [store.list(project)[0]["id"]]


def test_dedicated_worker_reviews_with_user_queue_backlog(store, project, tmp_path):
    app = application(store, project, tmp_path, proactive_evolution=False)
    task_id = app.submit("This queued user task must not be consumed by the review worker")
    stop, reports = threading.Event(), []

    def completed(report):
        reports.append(report)
        stop.set()

    ProactiveEvolution(app).run(stop, poll_interval=3600, on_cycle=completed)
    assert len(reports) == 1 and reports[0]["status"] == "succeeded"
    assert reports[0]["proposals"]
    assert app.state.task(task_id)["status"] == "queued"


def test_worker_stop_wakes_cooldown_wait_without_more_inference(store, project, tmp_path):
    app = application(store, project, tmp_path)
    worker = ProactiveEvolution(app)
    assert worker.once()["status"] == "succeeded"
    idle, stop = threading.Event(), threading.Event()
    original = worker.once

    def cooldown():
        assert original() is None
        idle.set()
        return None

    worker.once = cooldown
    reports = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.run, stop, poll_interval=3600, on_cycle=reports.append)
        try:
            assert idle.wait(5)
        finally:
            stop.set()
        future.result(timeout=5)
    assert reports == []


def test_stop_during_cycle_finishes_report_then_exits(store, project, tmp_path):
    app = application(store, project, tmp_path)
    stop = threading.Event()

    class StopDuringReview(ReviewModel):
        def complete(self, messages, tools):
            stop.set()
            return super().complete(messages, tools)

    app.model_factory = StopDuringReview
    reports = []
    ProactiveEvolution(app).run(stop, poll_interval=3600, on_cycle=reports.append)
    assert len(reports) == 1 and reports[0]["proposals"]
    assert ProactiveEvolution(app).status()["lease_until"] == 0


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True, "60"])
def test_worker_rejects_invalid_poll_interval(store, project, tmp_path, interval):
    app = application(store, project, tmp_path)
    with pytest.raises(ValueError, match="positive finite"):
        ProactiveEvolution(app).run(threading.Event(), poll_interval=interval)
    assert ProactiveEvolution(app).status() is None


def test_evolution_worker_cli_once_preserves_queue_and_cooldown(
    store, project, tmp_path, monkeypatch, capsys
):
    from evomesh import cli

    app = application(store, project, tmp_path)
    task_id = app.submit("Pending user work")
    monkeypatch.setattr(cli, "AgentApp", lambda *a, **kw: app)
    args = cli.parser().parse_args(["evolution-worker", "--once"])
    cli.dispatch(args, store, app.config)
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert app.state.task(task_id)["status"] == "queued"
    cli.dispatch(args, store, app.config)
    assert json.loads(capsys.readouterr().out) is None


def test_evolution_worker_cli_signal_finishes_cycle_and_restores_handlers(
    store, project, tmp_path, monkeypatch, capsys
):
    import signal

    from evomesh import cli

    app = application(store, project, tmp_path)
    previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    handlers = dict(previous)

    def install(signum, handler):
        old = handlers[signum]
        handlers[signum] = handler
        return old

    class StopModel(ReviewModel):
        def complete(self, messages, tools):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return super().complete(messages, tools)

    app.model_factory = StopModel
    monkeypatch.setattr(cli, "AgentApp", lambda *a, **kw: app)
    monkeypatch.setattr(signal, "signal", install)
    cli.dispatch(
        cli.parser().parse_args(["evolution-worker", "--interval", "3600"]), store, app.config
    )
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert ProactiveEvolution(app).status()["lease_until"] == 0
    assert handlers == previous
