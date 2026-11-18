import json
import os

import pytest
from jsonschema import ValidationError

from evomesh import AgentApp, Config
from evomesh.tools import DirectoryChanged, workspace_tools


def listing(root):
    return next(t for t in workspace_tools(root) if t.name == "list_files_page")


@pytest.mark.parametrize("suffix", ["normal", "\x01" * 100, "中文🙂" * 20])
def test_complete_directory_pages_preserve_names_and_json_budget(tmp_path, suffix):
    names = [f"{n:04}_{suffix}" for n in range(430)]
    for name in names:
        (tmp_path / name).touch()
    (tmp_path / "subdir").mkdir()
    names.append("subdir/")
    tool, offset, digest, seen = listing(tmp_path), 0, None, []
    while offset is not None:
        args = {"offset": offset, "limit": 200}
        if digest is not None:
            args["expected_sha256"] = digest
        raw = tool.invoke(json.dumps(args))
        assert len(raw) <= 18000
        page = json.loads(raw)
        assert page["total_entries"] == 431 and page["offset"] == offset
        assert page["entries"]
        if digest is not None:
            assert page["sha256"] == digest
        digest = page["sha256"]
        seen.extend(page["entries"])
        if page["next_offset"] is not None:
            assert page["next_offset"] == offset + len(page["entries"])
        offset = page["next_offset"]
    assert seen == names
    legacy = next(t for t in workspace_tools(tmp_path) if t.name == "list_files")
    assert json.loads(legacy.invoke('{"path":"."}')) == names[:200]


@pytest.mark.parametrize("count", [0, 100, 101])
def test_directory_page_boundary_and_nested_scope(tmp_path, count):
    folder = tmp_path / "nested"
    folder.mkdir()
    for n in range(count):
        (folder / f"{n:03}.txt").touch()
    tool = listing(tmp_path)
    page = json.loads(tool.invoke('{"path":"nested"}'))
    assert len(page["entries"]) == min(count, 100)
    assert page["next_offset"] == (100 if count > 100 else None)
    last = json.loads(tool.invoke(json.dumps({"path": "nested", "offset": count})))
    assert last["entries"] == [] and last["next_offset"] is None


@pytest.mark.parametrize("change", ["add", "remove", "rename", "type"])
def test_directory_changes_reject_stale_pagination(tmp_path, change):
    (tmp_path / "a.txt").touch()
    (tmp_path / "b.txt").touch()
    tool = listing(tmp_path)
    digest = json.loads(tool.invoke('{"limit":1}'))["sha256"]
    if change == "add":
        (tmp_path / "0.txt").touch()
    elif change == "remove":
        (tmp_path / "a.txt").unlink()
    elif change == "rename":
        (tmp_path / "a.txt").rename(tmp_path / "c.txt")
    else:
        (tmp_path / "a.txt").unlink()
        (tmp_path / "a.txt").mkdir()
    with pytest.raises(DirectoryChanged):
        tool.invoke(json.dumps({"offset": 1, "expected_sha256": digest}))
    assert json.loads(tool.invoke("{}"))["sha256"] != digest


def test_listing_excludes_hidden_symlink_and_special_entries(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "visible").write_text("before")
    (root / ".env").write_text("PRIVATE")
    (root / ".git").mkdir()
    (tmp_path / "outside").mkdir()
    (root / "outside-link").symlink_to(tmp_path / "outside", target_is_directory=True)
    (root / "internal-link").symlink_to(root / "visible")
    os.mkfifo(root / "pipe")
    tool = listing(root)
    page = json.loads(tool.invoke("{}"))
    assert page["entries"] == ["visible"]
    (root / "visible").write_text("after")
    assert json.loads(tool.invoke(json.dumps({"expected_sha256": page["sha256"]}))) == page
    for path in ["../outside", "outside-link", ".env", ".git", "visible", "missing"]:
        with pytest.raises(ValueError):
            tool.invoke(json.dumps({"path": path}))


@pytest.mark.parametrize(
    "args",
    [
        {"offset": -1},
        {"offset": True},
        {"offset": 1},
        {"limit": 0},
        {"limit": 201},
        {"limit": False},
    ],
)
def test_invalid_directory_pagination(tmp_path, args):
    with pytest.raises((ValueError, ValidationError)):
        listing(tmp_path).invoke(json.dumps(args))


def test_directory_change_reaches_assistant_and_private_evolution_evidence(
    tmp_path, store, project
):
    (tmp_path / "PRIVATE_ENTRY").touch()
    tool = listing(tmp_path)
    digest = json.loads(tool.invoke("{}"))["sha256"]
    (tmp_path / "new.txt").touch()

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                result = json.loads(messages[-1]["content"])
                assert result["error"] == "DirectoryChanged"
                assert "Restart at offset zero" in result["feedback"]
                return {"role": "assistant", "content": "Restart listing"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "list",
                        "type": "function",
                        "function": {
                            "name": "list_files_page",
                            "arguments": json.dumps({"offset": 1, "expected_sha256": digest}),
                        },
                    }
                ],
            }

    with AgentApp(Config(project=project, workspace=tmp_path), store=store, model=Model()) as app:
        assert app.run("Synthetic directory") == "Restart listing"
        assert app.last_run_metrics["tool_errors"] == 1
        assert app.last_run_metrics["tool_output_truncations"] == 0
    row = store.observations(project)[0]
    detail = json.loads(row["detail"])
    assert row["kind"] == "tool_error" and detail["error"] == "DirectoryChanged"
    assert detail["source_paths"] == ["src/evomesh/tools.py"]
    assert "PRIVATE_ENTRY" not in row["detail"] and digest not in row["detail"]
