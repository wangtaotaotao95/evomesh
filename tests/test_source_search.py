import json
import sys

import pytest

from evomesh import Agent
from evomesh.evolution import Integrator, git, revision, source_file
from evomesh.source_search import search_source_page


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "source"
    path.mkdir()
    git(path, "init", "-b", "main")
    return path


def snapshot(repo):
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Synthetic search fixture")
    base = revision(repo)
    paths = [
        row.split("\t", 1)[1] for row in git(repo, "ls-tree", "-r", "-z", base).split("\0") if row
    ]
    return base, paths


def call(name, **arguments):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "search-review",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_search_crosses_empty_scan_pages_and_keeps_pinned_revision(repo):
    for index in range(65):
        (repo / f"{index:03d}.py").write_text("# no match\n")
    (repo / "064.py").write_text("needle\nneedle\n")
    base, paths = snapshot(repo)
    (repo / "064.py").write_text("changed\n")
    snapshot(repo)
    page = search_source_page(repo, base, paths, "needle", limit=1)
    assert page["matches"] == [] and page["scanned_files"] == 50
    assert page["next_cursor"] == {"offset": 50, "line_offset": 0}
    matches = []
    while page["next_cursor"] is not None:
        page = search_source_page(repo, base, paths, "needle", limit=1, **page["next_cursor"])
        assert page["base_commit"] == base and page["scanned_files"] <= 50
        matches.extend(page["matches"])
    assert [(row["path"], row["line"]) for row in matches] == [("064.py", 1), ("064.py", 2)]


def test_json_bounded_pagination_preserves_matches_and_character_read_offsets(repo):
    name = "中文\tname\n.py"
    line = "你" * 600 + "\x01" * 500 + "needle" + "tail" * 200
    content = (line + "\r\n") * 25
    assert len(content.encode()) <= 100_000
    (repo / name).write_bytes(content.encode())
    base, paths = snapshot(repo)
    cursor, matches, page_count = {}, [], 0
    while cursor is not None:
        page = search_source_page(repo, base, paths, "needle", limit=100, **cursor)
        assert len(json.dumps(page, ensure_ascii=False)) <= 18000
        matches.extend(page["matches"])
        cursor = page["next_cursor"]
        page_count += 1
    assert page_count > 1 and len(matches) == 25
    assert [row["line"] for row in matches] == list(range(1, 26))
    for row in matches:
        assert row["path"] == name and row["column"] == 1101
        assert "needle" in row["text"] and row["text_truncated"]
        assert content[row["offset"] : row["offset"] + len(row["text"])] == row["text"]


def test_search_skips_ineligible_paths_and_is_literal(repo):
    (repo / "regular.py").write_text("needle.*\nNEEDLE.*\nneedle\n")
    (repo / "binary.py").write_bytes(b"needle\0")
    (repo / "invalid.py").write_bytes(b"needle\xff")
    (repo / "large.py").write_text("needle" * 20000)
    (repo / ".env").write_text("needle=synthetic-only\n")
    (repo / "link.py").symlink_to("regular.py")
    base, paths = snapshot(repo)
    page = search_source_page(repo, base, paths, "needle.*")
    assert page["skipped_files"] == 5 and page["scanned_files"] == 6
    assert len(page["matches"]) == 1 and page["matches"][0]["line"] == 1
    assert page["next_cursor"] is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "x" * 501},
        {"offset": -1},
        {"offset": 2},
        {"line_offset": -1},
        {"line_offset": 2},
        {"offset": 1, "line_offset": 1},
        {"limit": 0},
        {"limit": 101},
    ],
)
def test_search_rejects_invalid_cursors_and_limits(repo, arguments):
    (repo / "tool.py").write_text("needle\n")
    base, paths = snapshot(repo)
    with pytest.raises(ValueError):
        search_source_page(repo, base, paths, **{"query": "needle", **arguments})


def test_search_honors_review_lease_guard_between_files(repo):
    for name in ("a.py", "b.py"):
        (repo / name).write_text("needle\n")
    base, paths = snapshot(repo)
    checked = []

    def guard():
        checked.append(True)
        if len(checked) == 2:
            raise RuntimeError("Synthetic lease loss")

    with pytest.raises(RuntimeError, match="lease loss"):
        search_source_page(repo, base, paths, "needle", guard=guard)
    assert len(checked) == 2


def test_search_empty_scopes_and_errors_keep_source_provenance_private(store, project, repo):
    (repo / "tool.py").write_text("# PRIVATE_SEARCH_TERM\n")
    snapshot(repo)
    requests = [{"prefix": "tool*"}, {"offset": 1}, {"offset": 2}]

    class Reader:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools):
            if self.step:
                page = json.loads(messages[-1]["content"])
                if self.step < 3:
                    assert page["matches"] == [] and page["next_cursor"] is None
                else:
                    assert page["error"] == "ValueError"
                    return {"role": "assistant", "content": "Finished"}
            request = requests[self.step]
            self.step += 1
            return call("search_source", query="PRIVATE_SEARCH_TERM", **request)

    assert Agent(Reader(), store, [], project=project, repo=repo).reflect(proactive=True) == []
    rows = store.observations(project)
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["tool"] == "search_source" and detail["capability"] == "evolution"
    assert detail["source_paths"] == [
        "src/evomesh/agent.py",
        "src/evomesh/evolution.py",
        "src/evomesh/source_search.py",
    ]
    assert "PRIVATE_SEARCH_TERM" not in json.dumps(rows)


def test_search_requires_source_read_before_evidence_and_integrates_edit(store, project, repo):
    (repo / "tool.py").write_text("def answer():\n    return 1\n")
    (repo / "other.py").write_text("# return 1 outside selected prefix\n")
    base, _ = snapshot(repo)

    class Reviewer:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools):
            self.step += 1
            if self.step == 1:
                (repo / "tool.py").write_text("uncommitted user edit")
                return call("search_source", query="return 1", prefix="tool")
            if self.step == 2:
                page = json.loads(messages[-1]["content"])
                assert page["base_commit"] == base and page["total_paths"] == 1
                self.match = page["matches"][0]
                assert self.match["line"] == 2 and page["next_cursor"] is None
                return call("record_finding", detail="Premature finding", paths=["tool.py"])
            if self.step == 3:
                assert json.loads(messages[-1]["content"])["error"] == "ValueError"
                return call("read_source", path=self.match["path"], offset=self.match["offset"])
            if self.step == 4:
                assert "return 1" in json.loads(messages[-1]["content"])["content"]
                return call("record_finding", detail="Answer should be two", paths=["tool.py"])
            if self.step == 5:
                return call(
                    "propose_evolution",
                    target="tool",
                    rationale="Correct synthetic answer",
                    observation_ids=[messages[-1]["content"]],
                    files=[{"path": "tool.py", "edits": [{"old": "return 1", "new": "return 2"}]}],
                )
            return {"role": "assistant", "content": "Submitted"}

    identifiers = Agent(Reviewer(), store, [], project=project, repo=repo).reflect(proactive=True)
    result = Integrator(
        store,
        repo,
        project,
        [[sys.executable, "-c", "from tool import answer; assert answer() == 2"]],
    ).once()
    assert result["status"] == "integrated" and result["id"] == identifiers[0]
    assert b"return 2" in source_file(repo, result["commit"], "tool.py")
    assert (repo / "tool.py").read_text() == "uncommitted user edit"
    assert revision(repo) == base
    evidence = [row for row in store.observations(project) if row["kind"] == "source_review"]
    assert len(evidence) == 1 and "Premature" not in evidence[0]["detail"]
