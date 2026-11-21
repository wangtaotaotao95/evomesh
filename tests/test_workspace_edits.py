import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evomesh import AgentApp, Config
from evomesh.tools import FileVersionChanged, workspace_tools


def test_exact_edit_preserves_bom_unicode_and_mixed_line_endings(tmp_path):
    tools = {t.name: t for t in workspace_tools(tmp_path)}
    original = "\ufeff# 中文🙂\r\nvalue = 1\r\n# LF\n# CR\r# no final newline"
    path = tmp_path / "fixture.py"
    path.write_bytes(original.encode())
    assert tools["read_file"].invoke('{"path":"fixture.py"}') == original
    page = json.loads(tools["read_file_chunk"].invoke('{"path":"fixture.py"}'))
    result = tools["edit_file"].invoke(
        json.dumps(
            {
                "path": "fixture.py",
                "old": "value = 1\r\n",
                "new": "value = 2\r\n",
                "expected_sha256": page["sha256"],
            }
        )
    )
    assert result == "Saved fixture.py"
    assert path.read_bytes() == original.replace("value = 1", "value = 2").encode()
    with pytest.raises(FileVersionChanged):
        tools["edit_file"].invoke(
            json.dumps(
                {
                    "path": "fixture.py",
                    "old": "value = 2",
                    "new": "value = 3",
                    "expected_sha256": page["sha256"],
                }
            )
        )
    assert path.read_bytes() == original.replace("value = 1", "value = 2").encode()


@pytest.mark.parametrize(
    "content,old",
    [("aaa", "aa"), ("ababa", "aba"), ("one one", "one"), ("one", "missing"), ("one", "")],
)
def test_ambiguous_or_missing_edit_leaves_file_unchanged(tmp_path, content, old):
    path = tmp_path / "fixture.txt"
    path.write_bytes(content.encode())
    edit = next(t for t in workspace_tools(tmp_path) if t.name == "edit_file")
    with pytest.raises(ValueError, match="exactly one"):
        edit.invoke(json.dumps({"path": path.name, "old": old, "new": "changed"}))
    assert path.read_bytes() == content.encode()


@pytest.mark.parametrize(
    "raw,new",
    [(b"old\x00binary", "new"), (b"old\xff", "new"), (b"old", "x" * 100001), (b"old", "new\0")],
)
def test_invalid_or_oversized_edit_does_not_write(tmp_path, raw, new):
    path = tmp_path / "fixture.txt"
    path.write_bytes(raw)
    edit = next(t for t in workspace_tools(tmp_path) if t.name == "edit_file")
    with pytest.raises(ValueError):
        edit.invoke(json.dumps({"path": path.name, "old": "old", "new": new}))
    assert path.read_bytes() == raw


def test_stale_edit_rejected_and_source_evidence_excludes_private_content(store, project, tmp_path):
    path = tmp_path / "PRIVATE_FILENAME.txt"
    path.write_bytes(b"PRIVATE_OLD\r\nunchanged target\r\n")
    changed = b"PRIVATE_NEW\r\nunchanged target\r\n"

    class Model:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools):
            self.step += 1
            if self.step == 1:
                name, arguments = "read_file_chunk", {"path": path.name}
            elif self.step == 2:
                page = json.loads(messages[-1]["content"])
                path.write_bytes(changed)  # Another instance changed a different line.
                name, arguments = (
                    "edit_file",
                    {
                        "path": path.name,
                        "old": "unchanged target",
                        "new": "edited target",
                        "expected_sha256": page["sha256"],
                    },
                )
            else:
                assert json.loads(messages[-1]["content"])["error"] == "FileVersionChanged"
                return {"role": "assistant", "content": "Reread before editing"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(self.step),
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }

    with AgentApp(Config(project=project, workspace=tmp_path), store=store, model=Model()) as app:
        assert app.run("Synthetic edit") == "Reread before editing"
        assert app.last_run_metrics["tool_errors"] == 1
    assert path.read_bytes() == changed
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "tool_error"
    detail = json.loads(rows[0]["detail"])
    assert detail["error"] == "FileVersionChanged"
    assert detail["source_paths"] == ["src/evomesh/tools.py"]
    assert "PRIVATE_" not in json.dumps(rows)


def test_full_write_digest_preserves_bytes_and_default_overwrite(tmp_path):
    tools = {t.name: t for t in workspace_tools(tmp_path)}
    tools["write_file"].invoke('{"path":"nested/note.txt","content":"old"}')
    path = tmp_path / "nested" / "note.txt"
    page = json.loads(tools["read_file_chunk"].invoke('{"path":"nested/note.txt"}'))
    content = "\ufeff中文🙂\r\nnext\rlast\n"
    assert (
        tools["write_file"].invoke(
            json.dumps(
                {"path": "nested/note.txt", "content": content, "expected_sha256": page["sha256"]}
            )
        )
        == "Saved nested/note.txt"
    )
    assert path.read_bytes() == content.encode()
    with pytest.raises(FileVersionChanged):
        tools["write_file"].invoke(
            json.dumps(
                {"path": "nested/note.txt", "content": "stale", "expected_sha256": page["sha256"]}
            )
        )
    assert path.read_bytes() == content.encode()
    tools["write_file"].invoke('{"path":"nested/note.txt","content":"explicit replacement"}')
    assert path.read_text() == "explicit replacement"


def test_competing_create_only_writers_have_one_winner(tmp_path):
    writers = [
        next(t for t in workspace_tools(tmp_path) if t.name == "write_file") for _ in range(2)
    ]
    contents = ["left" * 1000, "right" * 1000]

    def create(index):
        try:
            writers[index].invoke(
                json.dumps(
                    {"path": "nested/new.txt", "content": contents[index], "create_only": True}
                )
            )
        except FileExistsError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(results) == 1
    assert (tmp_path / "nested" / "new.txt").read_text() == contents[results.index(True)]


@pytest.mark.parametrize("kind", ["regular", "directory", "fifo", "symlink", "dangling"])
def test_create_only_never_replaces_existing_destination(tmp_path, kind):
    destination = tmp_path / "target"
    if kind == "regular":
        destination.write_text("original")
    elif kind == "directory":
        destination.mkdir()
    elif kind == "fifo":
        os.mkfifo(destination)
    else:
        if kind == "symlink":
            (tmp_path / "linked.txt").write_text("original")
        destination.symlink_to("linked.txt")
    writer = next(t for t in workspace_tools(tmp_path) if t.name == "write_file")
    with pytest.raises(FileExistsError):
        writer.invoke('{"path":"target","content":"replacement","create_only":true}')
    if kind in {"regular", "symlink"}:
        assert destination.read_text() == "original"
    if kind == "dangling":
        assert destination.is_symlink() and not (tmp_path / "linked.txt").exists()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_default_write_rejects_special_file_before_open(tmp_path, monkeypatch, kind):
    destination = tmp_path / "target"
    if kind == "directory":
        destination.mkdir()
    else:
        os.mkfifo(destination)
    writer = next(t for t in workspace_tools(tmp_path) if t.name == "write_file")

    def no_open(*args, **kwargs):
        pytest.fail("Special destinations must not be opened, including blocking FIFOs")

    monkeypatch.setattr(Path, "open", no_open)
    with pytest.raises(ValueError, match="regular"):
        writer.invoke('{"path":"target","content":"replacement"}')


def test_write_guards_fail_before_creating_directories_or_changing_content(tmp_path):
    writer = next(t for t in workspace_tools(tmp_path) if t.name == "write_file")
    digest = hashlib.sha256(b"original").hexdigest()
    with pytest.raises(FileVersionChanged):
        writer.handler("missing/note.txt", "new", expected_sha256=digest)
    assert not (tmp_path / "missing").exists()
    destination = tmp_path / "note.txt"
    destination.write_text("original")
    for options in ({"create_only": True, "expected_sha256": digest}, {"create_only": "yes"}):
        with pytest.raises(ValueError):
            writer.handler("note.txt", "new", **options)
    with pytest.raises(ValueError, match="binary"):
        writer.handler("note.txt", "new\0")
    assert destination.read_text() == "original"


@pytest.mark.parametrize(
    "mode,error", [("stale", "FileVersionChanged"), ("create", "FileExistsError")]
)
def test_write_guard_failures_are_private_evolution_evidence(store, project, tmp_path, mode, error):
    path = tmp_path / "PRIVATE_FILE.txt"
    path.write_text("PRIVATE_ORIGINAL")

    class Model:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools):
            self.step += 1
            if self.step == 1:
                name, args = "read_file_chunk", {"path": path.name}
            elif self.step == 2:
                digest = json.loads(messages[-1]["content"])["sha256"]
                path.write_text("PRIVATE_CONCURRENT_CHANGE")
                name = "write_file"
                args = {"path": path.name, "content": "PRIVATE_REPLACEMENT"}
                args.update(
                    {"expected_sha256": digest} if mode == "stale" else {"create_only": True}
                )
            else:
                assert json.loads(messages[-1]["content"])["error"] == error
                return {"role": "assistant", "content": "Reread before replacing"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(self.step),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }

    with AgentApp(Config(project=project, workspace=tmp_path), store=store, model=Model()) as app:
        assert app.run("Synthetic guarded write") == "Reread before replacing"
        assert app.last_run_metrics["tool_errors"] == 1
    assert path.read_text() == "PRIVATE_CONCURRENT_CHANGE"
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "tool_error"
    detail = json.loads(rows[0]["detail"])
    assert detail["tool"] == "write_file" and detail["error"] == error
    assert detail["source_paths"] == ["src/evomesh/tools.py"]
    assert "PRIVATE_" not in json.dumps(rows)
