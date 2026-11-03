import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from evomesh.store import LeaseLost, proposals


def add(store, project, content="hello"):
    return store.submit(
        project=project,
        instance="a",
        target="soul",
        base_commit="a" * 40,
        rationale="A useful improvement",
        changes=[{"content": content}],
    )


def test_duplicate_proposals_across_instances(store, project):
    first = add(store, project)
    assert add(store, project) == first
    assert len(store.list(project)) == 1


def test_claim_exclusive_across_connections(store, project):
    identifier = add(store, project)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim(project), range(8)))
    claimed = [r for r in results if r]
    assert len(claimed) == 1
    assert claimed[0]["id"] == identifier


def test_expiry_recovery_rejects_stale_token(store, project):
    identifier = add(store, project)
    old = store.claim(project)
    with store.engine.begin() as conn:
        conn.execute(
            update(proposals)
            .where(proposals.c.id == identifier)
            .values(lease_until=time.time() - 1)
        )
    new = store.claim(project)
    assert old["token"] != new["token"]
    with pytest.raises(LeaseLost):
        store.finish(identifier, old["token"], "integrated")
    store.renew(identifier, new["token"])
    store.finish(identifier, new["token"], "integrated", result_commit="b" * 40)
    assert store.list(project)[0]["attempts"] == 2


def test_project_isolation(store, project):
    add(store, project)
    assert store.claim(project + "-other") is None
    store.observe(project, "instance-a", "feedback", "Repeated tool failure")
    assert len(store.observations(project)) == 1
    assert not store.observations(project + "-other")
