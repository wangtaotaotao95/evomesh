import json
import sys

from sqlalchemy import update

from evomesh import Agent
from evomesh.context import request_size
from evomesh.evolution import Integrator, git, make_change, revision, source_file, submit
from evomesh.store import observations, proposals


def source_repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "tool.py").write_text("def answer():\n    return 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path


def call(name, **arguments):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "review",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


def test_large_review_context_pages_complete_frozen_records(store, project, tmp_path):
    repo = source_repo(tmp_path)
    detail = '\x01\t\\"' * 3998 + "OBS_END"
    rationale = '\x01\t\\"' * 2498 + "WHY_END"
    report = '\x01\t\\"' * 3998 + "GATE_END"
    evidence = [store.observe(project, "replica", "tool_error", detail) for _ in range(20)]
    identifiers = [
        store.submit(
            project=project,
            instance="replica",
            target="tool",
            base_commit=revision(repo),
            rationale=rationale,
            changes=[{"fixture": index}],
            observation_ids=[evidence[-1]],
        )
        for index in range(10)
    ]
    for _ in identifiers:
        claimed = store.claim(project)
        store.finish(claimed["id"], claimed["token"], "rejected", report)
    old_context = json.dumps(store.observations(project)) + json.dumps(
        [
            {key: row[key] for key in ("id", "target", "status", "rationale")}
            for row in store.list(project, limit=10)
        ]
    )
    assert len(old_context) > 120000
    requested = [
        ("observation", evidence[-1], "detail", detail),
        ("proposal", identifiers[-1], "rationale", rationale),
        ("proposal", identifiers[-1], "report", report),
    ]

    class Reader:
        def __init__(self):
            self.index, self.offset, self.parts = 0, 0, []

        def complete(self, messages, tools):
            assert request_size(messages, tools) <= 120000
            if messages[-1]["role"] == "user":
                raw = messages[-1]["content"].split("avoid duplicates):\n", 1)[1]
                assert len(raw) <= 18000
                summary = json.loads(raw)
                assert {r["id"] for r in summary["observations"]} == set(evidence)
                assert {r["id"] for r in summary["proposals"]} == set(identifiers)
                assert all(r["detail_truncated"] for r in summary["observations"])
                assert all(r["rationale_truncated"] for r in summary["proposals"])
                assert all(r["report_chars"] == len(report) for r in summary["proposals"])
                # Other replicas may update the ledger during a review. All pages
                # must still refer to the snapshot advertised to this model.
                with store.engine.begin() as conn:
                    conn.execute(
                        update(observations)
                        .where(observations.c.id == evidence[-1])
                        .values(detail="changed after snapshot")
                    )
                    conn.execute(
                        update(proposals)
                        .where(proposals.c.id == identifiers[-1])
                        .values(report="changed after snapshot")
                    )
            else:
                raw = messages[-1]["content"]
                assert len(raw) <= 18000
                page = json.loads(raw)
                assert page["offset"] == self.offset
                assert page["total_chars"] == len(requested[self.index][3])
                if self.index > 0:
                    assert page["metadata"]["status"] == "rejected"
                    assert page["metadata"]["observation_ids"] == [evidence[-1]]
                    assert "changes" not in page["metadata"] and "token" not in page["metadata"]
                self.parts.append(page["content"])
                self.offset = page["next_offset"]
                if self.offset is None:
                    assert "".join(self.parts) == requested[self.index][3]
                    self.index += 1
                    self.parts, self.offset = [], 0
                    if self.index == len(requested):
                        return {"role": "assistant", "content": "Inspected complete evidence"}
            kind, identifier, field, _ = requested[self.index]
            return call(
                "read_review_record",
                record_type=kind,
                record_id=identifier,
                field=field,
                offset=self.offset,
            )

    reader = Reader()
    assert Agent(reader, store, [], project=project, repo=repo, max_steps=64).reflect() == []
    assert reader.index == 3
    assert not any(row["kind"] == "tool_output_limit" for row in store.observations(project))


def test_review_record_rejects_outside_snapshot_and_wrong_fields(store, project, tmp_path):
    repo = source_repo(tmp_path)
    old = store.observe(project, "replica", "feedback", "old fixture")
    for _ in range(20):
        current = store.observe(project, "replica", "feedback", "current fixture")
    foreign = store.observe(project + "-other", "replica", "feedback", "FOREIGN_PRIVATE_FIXTURE")
    requests = [
        {"record_id": foreign},
        {"record_id": old},
        {"field": "report"},
        {"offset": -1},
        {"offset": 10000},
        {"limit": 0},
        {"limit": 12001},
    ]

    class Invalid:
        def __init__(self):
            self.count = 0

        def complete(self, messages, tools):
            assert "FOREIGN_PRIVATE_FIXTURE" not in json.dumps(messages)
            if messages[-1]["role"] == "tool":
                assert json.loads(messages[-1]["content"])["error"] in {
                    "ValueError",
                    "ValidationError",
                }
            if self.count == len(requests):
                return {"role": "assistant", "content": "No proposal"}
            args = {
                "record_type": "observation",
                "record_id": current,
                "field": "detail",
                **requests[self.count],
            }
            self.count += 1
            return call("read_review_record", **args)

    assert Agent(Invalid(), store, [], project=project, repo=repo).reflect() == []
    assert store.list(project) == []


def test_rejected_proposal_feedback_guides_corrected_gated_change(store, project, tmp_path):
    repo = source_repo(tmp_path)
    base = revision(repo)
    evidence = store.observe(project, "replica", "feedback", "answer should return 2")
    rejected = submit(
        store,
        repo,
        project=project,
        instance="replica",
        target="tool",
        rationale="Wrong attempt",
        changes=[make_change(repo, base, "tool.py", "def answer():\n    return 3\n")],
        observation_ids=[evidence],
    )
    checks = [
        [sys.executable, "-c", "from tool import answer; assert answer() == 2, 'EXPECTED_TWO'"]
    ]
    integrator = Integrator(store, repo, project, checks)
    assert integrator.once()["status"] == "rejected"

    class Corrector:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools):
            self.step += 1
            if self.step == 1:
                return call(
                    "read_review_record", record_type="proposal", record_id=rejected, field="report"
                )
            if self.step == 2:
                page = json.loads(messages[-1]["content"])
                assert "EXPECTED_TWO" in page["content"]
                assert page["metadata"]["observation_ids"] == [evidence]
                return call("read_source", path="tool.py")
            if self.step == 3:
                return call(
                    "propose_evolution",
                    target="tool",
                    rationale="Meet the unchanged gate",
                    observation_ids=[evidence],
                    files=[
                        {
                            "path": "tool.py",
                            "edits": [{"old": "return 1", "new": "return 2"}],
                        }
                    ],
                )
            return {"role": "assistant", "content": "Submitted corrected proposal"}

    identifiers = Agent(Corrector(), store, [], project=project, repo=repo).reflect(proactive=True)
    assert len(identifiers) == 1 and identifiers[0] != rejected
    result = integrator.once()
    assert result["status"] == "integrated"
    assert source_file(repo, result["commit"], "tool.py") == b"def answer():\n    return 2\n"
    assert revision(repo) == base  # Explicit deployment still required.
    rows = {row["id"]: row for row in store.list(project)}
    assert rows[rejected]["status"] == "rejected"
    assert rows[identifiers[0]]["observation_ids"] == [evidence]
