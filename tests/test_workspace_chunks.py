import hashlib
import json

import pytest

from evomesh import AgentApp, Config
from evomesh.tools import FileVersionChanged, workspace_tools


def reader(root):
    return next(tool for tool in workspace_tools(root) if tool.name == "read_file_chunk")


@pytest.mark.parametrize(
    "content",
    [
        "line = 'hello'\r\n" * 2400 + "尾部🙂\n",
        '\x01\t\\"中' * 6000,
        "",
    ],
)
def test_complete_chunk_read_preserves_text_and_bounded_json(tmp_path, content):
    raw = content.encode("utf-8")
    (tmp_path / "notes.txt").write_bytes(raw)
    read = reader(tmp_path)
    offset, digest, chunks = 0, None, []
    while offset is not None:
        arguments = {"path": "notes.txt", "offset": offset}
        if digest:
            arguments["expected_sha256"] = digest
        output = read.invoke(json.dumps(arguments))
        assert len(output) <= 18000
        page = json.loads(output)
        assert page["offset"] == offset and page["total_chars"] == len(content)
        assert page["sha256"] == hashlib.sha256(raw).hexdigest()
        chunks.append(page["content"])
        if page["next_offset"] is not None:
            assert page["next_offset"] > offset
        offset, digest = page["next_offset"], page["sha256"]
    assert "".join(chunks) == content


def test_modified_file_rejects_stale_digest_and_allows_restart(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"old\r\ncontent")
    read = reader(tmp_path)
    first = json.loads(read.invoke('{"path":"notes.txt","limit":3}'))
    path.write_bytes(b"new\r\ncontent")
    with pytest.raises(FileVersionChanged):
        read.invoke(
            json.dumps(
                {
                    "path": "notes.txt",
                    "offset": first["next_offset"],
                    "expected_sha256": first["sha256"],
                }
            )
        )
    restarted = json.loads(read.invoke('{"path":"notes.txt"}'))
    assert restarted["content"] == "new\r\ncontent"
    assert restarted["sha256"] != first["sha256"]


def test_chunk_read_rejects_private_escape_binary_and_large_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("outside")
    (workspace / "escape").symlink_to(tmp_path / "outside.txt")
    (workspace / ".env").write_text("private")
    (workspace / "private-link").symlink_to(workspace / ".env")
    (workspace / "binary").write_bytes(b"text\0binary")
    (workspace / "invalid-utf8").write_bytes(b"\xff")
    (workspace / "large").write_bytes(b"x" * 100001)
    read = reader(workspace)
    for path in (
        "../outside.txt",
        "escape",
        ".env",
        "private-link",
        "binary",
        "invalid-utf8",
        "large",
        ".",
    ):
        with pytest.raises(ValueError):
            read.invoke(json.dumps({"path": path}))


@pytest.mark.parametrize(
    "arguments",
    [
        {"offset": -1},
        {"offset": 6},
        {"offset": True},
        {"limit": 0},
        {"limit": 12001},
        {"expected_sha256": "invalid"},
    ],
)
def test_invalid_chunk_ranges_are_rejected(tmp_path, arguments):
    from jsonschema import ValidationError

    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises((ValueError, ValidationError)):
        reader(tmp_path).invoke(json.dumps({"path": "notes.txt", **arguments}))


def test_agent_chunk_change_records_source_evidence_without_private_data(store, project, tmp_path):
    path = tmp_path / "private-name.txt"
    path.write_text("PRIVATE_CONTENT fixture")

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                page = json.loads(messages[-1]["content"])
                if "error" in page:
                    assert page["error"] == "FileVersionChanged"
                    return {"role": "assistant", "content": "File changed; restart reading"}
                path.write_text("modified fixture")
                args = {
                    "path": path.name,
                    "offset": page["next_offset"],
                    "expected_sha256": page["sha256"],
                }
            else:
                assert any(t["function"]["name"] == "read_file_chunk" for t in tools)
                args = {"path": path.name, "limit": 3}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "chunk",
                        "type": "function",
                        "function": {
                            "name": "read_file_chunk",
                            "arguments": json.dumps(args),
                        },
                    }
                ],
            }

    with AgentApp(Config(project=project, workspace=tmp_path), store=store, model=Model()) as app:
        assert app.run("Read the file") == "File changed; restart reading"
        assert app.last_run_metrics["tool_errors"] == 1
    observation = store.observations(project)[0]
    detail = json.loads(observation["detail"])
    assert detail["error"] == "FileVersionChanged"
    assert detail["source_paths"] == ["src/evomesh/tools.py"]
    assert "PRIVATE_CONTENT" not in observation["detail"]
    assert path.name not in observation["detail"]
