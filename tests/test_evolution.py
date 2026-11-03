import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from evomesh.evolution import Integrator, git, make_change, revision, source_file, submit
from evomesh.store import proposals


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "SOUL.md").write_text("Be helpful.\n")
    (path / "tool.py").write_text("def answer():\n    return 1\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "Initial")
    return path


def proposal(store, repo, project, path="SOUL.md", content="Be helpful and precise.\n"):
    base = revision(repo)
    return submit(
        store,
        repo,
        project=project,
        instance="instance-a",
        target="code",
        rationale="Reduce observed friction",
        base=base,
        changes=[make_change(repo, base, path, content)],
    )


def worker(store, repo, project, code="print('validated')", **kwargs):
    return Integrator(store, repo, project, [[sys.executable, "-c", code]], **kwargs)


def test_integrates_in_separate_branch(store, repo, project):
    before = revision(repo)
    identifier = proposal(store, repo, project)
    result = worker(store, repo, project).once()
    assert result["status"] == "integrated"
    assert revision(repo) == before
    assert (repo / "SOUL.md").read_text() == "Be helpful.\n"
    assert source_file(repo, result["commit"], "SOUL.md") == b"Be helpful and precise.\n"
    assert identifier in git(repo, "log", "evolution/main", "-1", "--format=%B")
    assert not git(repo, "status", "--porcelain")


def test_validation_failure_never_publishes(store, repo, project):
    before = revision(repo)
    proposal(store, repo, project)
    result = worker(store, repo, project, "raise SystemExit(1)").once()
    assert result["status"] == "rejected"
    assert revision(repo, "evolution/main") == before


def test_conflicts_and_disjoint_changes(store, repo, project):
    proposal(store, repo, project)
    proposal(store, repo, project, content="Different soul\n")
    proposal(store, repo, project, "tool.py", "def answer():\n    return 2\n")
    integrator = worker(store, repo, project)
    assert integrator.once()["status"] == "integrated"
    assert integrator.once()["status"] == "conflict"
    assert integrator.once()["status"] == "integrated"


def test_recovers_commit_when_db_finish_was_lost(store, repo, project):
    identifier = proposal(store, repo, project)
    integrator = worker(store, repo, project)
    first = integrator.once()
    with store.engine.begin() as conn:
        conn.execute(
            update(proposals)
            .where(proposals.c.id == identifier)
            .values(status="validating", token="dead-worker", lease_until=time.time() - 1)
        )
    recovered = integrator.once()
    assert recovered["commit"] == first["commit"]
    assert recovered["report"] == "Recovered published commit"


def test_two_integrators_retry_after_ref_race(store, repo, project):
    proposal(store, repo, project)
    proposal(store, repo, project, "tool.py", "def answer():\n    return 3\n")
    integrator = worker(store, repo, project, "import time; time.sleep(0.1)")
    integrator._head()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: integrator.once(), range(2)))
    assert any(r and r["status"] == "integrated" for r in results)
    for _ in range(3):
        integrator.once()
    assert {r["status"] for r in store.list(project)} == {"integrated"}
    head = revision(repo, "evolution/main")
    assert source_file(repo, head, "tool.py") == b"def answer():\n    return 3\n"


@pytest.mark.parametrize("path", ["../outside", "/tmp/escape", ".git/config", ".env", "a/../b"])
def test_rejects_invalid_paths(store, repo, project, path):
    with pytest.raises(ValueError):
        proposal(store, repo, project, path)


def test_rejects_symlink_parent(store, repo, project):
    (repo / "link").symlink_to(repo.parent, target_is_directory=True)
    git(repo, "add", "link")
    git(repo, "commit", "-m", "Add symlink")
    proposal(store, repo, project, "link/escape", "bad")
    assert worker(store, repo, project).once()["status"] == "rejected"
    assert not (repo.parent / "escape").exists()


def test_add_delete_and_validation_mutation(store, repo, project):
    base = revision(repo)
    submit(
        store,
        repo,
        project=project,
        instance="a",
        target="tool",
        rationale="Replace tool",
        changes=[
            make_change(repo, base, "tool.py", None),
            make_change(repo, base, "tools/new.py", "print(2)\n"),
        ],
    )
    result = worker(store, repo, project).once()
    assert result["status"] == "integrated"
    assert source_file(repo, result["commit"], "tool.py") is None
    assert source_file(repo, result["commit"], "tools/new.py") == b"print(2)\n"
    proposal(store, repo, project)
    result = worker(
        store, repo, project, "from pathlib import Path; Path('SOUL.md').write_text('tampered')"
    ).once()
    assert result["status"] == "rejected"


def test_timeout_and_checked_out_branch(store, repo, project):
    proposal(store, repo, project)
    result = worker(store, repo, project, "import time; time.sleep(5)", timeout=0.05).once()
    assert result["status"] == "rejected"
    with pytest.raises(ValueError, match="checked out"):
        worker(store, repo, project, branch="main").once()


def test_candidate_code_is_imported_and_credentials_removed(store, repo, project, monkeypatch):
    monkeypatch.setenv("EVOMESH_API_KEY", "test-secret")
    proposal(store, repo, project, "src/candidate.py", "VALUE = 42\n")
    result = worker(
        store,
        repo,
        project,
        "import candidate, os; assert candidate.VALUE == 42; "
        "assert 'EVOMESH_API_KEY' not in os.environ",
    ).once()
    assert result["status"] == "integrated"


def test_validator_sees_candidate_loop_source(store, repo, project):
    proposal(store, repo, project, "src/loop.py", "raise RuntimeError('broken loop')\n")
    assert worker(store, repo, project, "import loop").once()["status"] == "rejected"
