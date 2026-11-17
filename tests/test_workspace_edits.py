import json

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
    "raw,new", [(b"old\x00binary", "new"), (b"old\xff", "new"), (b"old", "x" * 100001)]
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
