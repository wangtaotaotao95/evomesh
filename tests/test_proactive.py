import json
import sys
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
