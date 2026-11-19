import json

import pytest
from jsonschema import ValidationError

from evomesh import AgentApp, Config
from evomesh.tools import workspace_tools


def search(root):
    return next(tool for tool in workspace_tools(root) if tool.name == "search_files")


@pytest.mark.parametrize(
    "suffix", ["x" * 490, "\x01" * 490, "中文🙂" * 100], ids=["plain", "escaped", "unicode"]
)
def test_complete_search_pages_are_valid_and_preserve_all_citations(tmp_path, suffix):
    line = "needle " + suffix
    names = ["a.txt", "z.txt", "sub/b.txt"]
    for name in names:
        file = tmp_path / name
        file.parent.mkdir(exist_ok=True)
        file.write_text((line + "\n") * 60, encoding="utf-8")
    tool, offset, matches = search(tmp_path), 0, []
    while offset is not None:
        raw = tool.invoke(json.dumps({"query": "needle", "offset": offset}))
        assert len(raw) <= 18000
        page = json.loads(raw)
        assert page["matches"]
        assert not page["scan_limit_reached"]
        assert not tool.result_is_truncated(raw)  # A next page is recoverable, not lost output.
        matches.extend(page["matches"])
        if page["next_offset"] is not None:
            assert page["next_offset"] == offset + len(page["matches"])
        assert page["truncated"] is (page["next_offset"] is not None)
        offset = page["next_offset"]
    assert matches == [
        {"path": name, "line": number, "text": line} for name in names for number in range(1, 61)
    ]


@pytest.mark.parametrize("count", [0, 100, 101])
def test_single_file_scope_and_exact_page_boundary(tmp_path, count):
    (tmp_path / "notes.txt").write_text("needle\n" * count)
    tool = search(tmp_path)
    first = json.loads(tool.invoke('{"query":"needle","path":"notes.txt"}'))
    assert len(first["matches"]) == min(count, 100)
    assert first["scanned_files"] == 1 and first["skipped_files"] == 0
    assert first["truncated"] is (count > 100)
    assert first["next_offset"] == (100 if count > 100 else None)
    if count > 100:
        last = json.loads(tool.invoke('{"query":"needle","path":"notes.txt","offset":100}'))
        assert last["matches"] == [{"path": "notes.txt", "line": 101, "text": "needle"}]
        assert last["next_offset"] is None and not last["truncated"]


def test_invalid_scope_and_unsearchable_files_are_explicit(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("needle PRIVATE")
    (workspace / "escape").symlink_to(tmp_path / "outside.txt")
    (workspace / ".env").write_text("needle PRIVATE")
    (workspace / "private-link").symlink_to(workspace / ".env")
    (workspace / "binary").write_bytes(b"needle\0binary")
    (workspace / "invalid").write_bytes(b"needle\xff")
    (workspace / "large").write_bytes(b"x" * 100001)
    (workspace / "visible.txt").write_text("needle public")
    tool = search(workspace)
    for path in ("missing", "../outside.txt", "escape", ".env", "private-link"):
        with pytest.raises(ValueError):
            tool.invoke(json.dumps({"query": "needle", "path": path}))
    page = json.loads(tool.invoke('{"query":"needle"}'))
    assert page["matches"] == [{"path": "visible.txt", "line": 1, "text": "needle public"}]
    assert page["skipped_files"] == 5
    assert page["scanned_files"] == 6
    assert "PRIVATE" not in json.dumps(page)


@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": -1},
        {"offset": True},
        {"offset": 100000001},
        {"limit": 0},
        {"limit": 101},
    ],
)
def test_invalid_pagination_arguments(tmp_path, arguments):
    with pytest.raises((ValueError, ValidationError)):
        search(tmp_path).invoke(json.dumps({"query": "needle", **arguments}))


def test_scan_limit_can_be_narrowed_and_records_private_evolution_evidence(
    tmp_path, store, project
):
    for number in range(1000):
        (tmp_path / f"{number:04}.txt").write_text("unrelated")
    folder = tmp_path / "private-folder"
    folder.mkdir()
    (folder / "answer.txt").write_text("PRIVATE_NEEDLE")

    class Model:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools):
            self.step += 1
            if self.step == 2:
                page = json.loads(messages[-1]["content"])
                assert page["matches"] == [] and page["scan_limit_reached"]
                assert page["next_offset"] is None and page["truncated"]
                assert page["scanned_files"] == 1000
            if self.step == 3:
                page = json.loads(messages[-1]["content"])
                assert page["matches"] == [
                    {
                        "path": "private-folder/answer.txt",
                        "line": 1,
                        "text": "PRIVATE_NEEDLE",
                    }
                ]
                assert not page["scan_limit_reached"] and page["next_offset"] is None
                return {"role": "assistant", "content": "Found after narrowing the search"}
            args = {"query": "PRIVATE_NEEDLE", "path": "." if self.step == 1 else "private-folder"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "search",
                        "type": "function",
                        "function": {
                            "name": "search_files",
                            "arguments": json.dumps(args),
                        },
                    }
                ],
            }

    with AgentApp(Config(project=project, workspace=tmp_path), store=store, model=Model()) as app:
        assert app.run("Search the workspace") == "Found after narrowing the search"
        assert app.last_run_metrics["tool_output_truncations"] == 1
        assert app.last_run_metrics["tool_errors"] == 0
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "tool_output_limit"
    detail = json.loads(rows[0]["detail"])
    assert detail["tool"] == "search_files" and detail["tool_truncated"]
    assert detail["source_paths"] == ["src/evomesh/tools.py"]
    assert "PRIVATE_NEEDLE" not in rows[0]["detail"]
    assert "private-folder" not in rows[0]["detail"]


@pytest.mark.parametrize(
    "prefix,query,suffix",
    [
        ("x" * 900, "needle", " tail"),
        ("x" * 495, "needle", "y" * 600),
        ("", "needle", "y" * 800),
        ("中文🙂" * 200, "匹配🙂", "末尾" * 300),
        ("x" * 600, "q" * 450, "y" * 600),
        ("x" * 600, "q" * 700, "y" * 600),
    ],
    ids=["late", "boundary", "start", "unicode", "wide-query", "oversized-query"],
)
def test_long_line_search_preview_contains_first_match_and_coordinates(
    tmp_path, prefix, query, suffix
):
    line = prefix + query + suffix
    (tmp_path / "notes.txt").write_text("unrelated\n" + line + "\n", encoding="utf-8")
    raw = search(tmp_path).invoke(json.dumps({"query": query}))
    assert len(raw) <= 18000
    page = json.loads(raw)
    match = page["matches"][0]
    assert match["path"] == "notes.txt" and match["line"] == 2
    assert match["column"] == len(prefix) + 1 and match["match_chars"] == len(query)
    assert match["line_chars"] == len(line) and match["text_truncated"]
    assert len(match["text"]) <= 500
    assert match["text"] == line[match["text_offset"] : match["text_offset"] + 500]
    if len(query) <= 500:
        assert query in match["text"]
    else:
        assert match["text"] == query[:500]
    assert not page["truncated"]  # Line excerpt clipping is separate from result pagination.


def test_long_line_search_pages_keep_escaped_evidence_and_first_occurrence(tmp_path):
    line = "\x01" * 600 + "needle" + "\x01" * 600 + "needle"
    (tmp_path / "notes.txt").write_text((line + "\n") * 60)
    tool, offset, matches = search(tmp_path), 0, []
    while offset is not None:
        raw = tool.invoke(json.dumps({"query": "needle", "offset": offset}))
        assert len(raw) <= 18000
        page = json.loads(raw)
        assert page["matches"] and not tool.result_is_truncated(raw)
        matches.extend(page["matches"])
        offset = page["next_offset"]
    assert [m["line"] for m in matches] == list(range(1, 61))
    assert all(m["column"] == 601 and m["text_offset"] == 500 for m in matches)
    assert all("needle" in m["text"] and m["text_truncated"] for m in matches)


def test_assistant_receives_late_match_without_false_output_limit(store, project, tmp_path):
    (tmp_path / "note.txt").write_text("x" * 900 + "PRIVATE_NEEDLE")

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                match = json.loads(messages[-1]["content"])["matches"][0]
                assert "PRIVATE_NEEDLE" in match["text"] and match["column"] == 901
                return {"role": "assistant", "content": "Located the matching text"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "search",
                        "type": "function",
                        "function": {
                            "name": "search_files",
                            "arguments": '{"query":"PRIVATE_NEEDLE"}',
                        },
                    }
                ],
            }

    with AgentApp(Config(project=project, workspace=tmp_path), store=store, model=Model()) as app:
        assert app.run("Synthetic search") == "Located the matching text"
        assert app.last_run_metrics["tool_errors"] == 0
        assert app.last_run_metrics["tool_output_truncations"] == 0
    assert store.observations(project) == []
