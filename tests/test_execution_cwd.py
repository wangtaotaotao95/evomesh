import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evomesh import AgentApp, Config
from evomesh.capabilities import ExecutionTimeout, execute_code_tool, terminal_tool


def arguments(kind, code, **options):
    return {
        **({"command": [sys.executable, "-c", code]} if kind == "terminal" else {"code": code}),
        **options,
    }


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
def test_cwd_is_per_call_and_concurrent_calls_do_not_change_host_directory(tmp_path, kind):
    tool = terminal_tool(tmp_path) if kind == "terminal" else execute_code_tool(tmp_path)
    (tmp_path / "note.txt").write_text("root")
    for name in ("left", "子 dir"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "note.txt").write_text(name)
    (tmp_path / "alias").symlink_to("子 dir", target_is_directory=True)
    code = "from pathlib import Path; print(Path('note.txt').read_text())"

    def run(cwd):
        return json.loads(tool.invoke(json.dumps(arguments(kind, code, cwd=cwd))))

    before = Path.cwd()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["left", "alias"]))
    assert [row["output"].strip() for row in results] == ["left", "子 dir"]
    assert all(row["exit_code"] == 0 for row in results)
    default = json.loads(tool.invoke(json.dumps(arguments(kind, code))))
    assert default["output"] == "root\n" and Path.cwd() == before


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
@pytest.mark.parametrize("backend", ["local", "docker"])
def test_invalid_cwd_never_starts_a_process(tmp_path, monkeypatch, kind, backend):
    root = tmp_path / "workspace"
    factory = terminal_tool if kind == "terminal" else execute_code_tool
    tool = factory(root, backend=backend)
    (root / "file.txt").write_text("synthetic")
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)

    def no_process(*args, **kwargs):
        pytest.fail("Invalid cwd must fail before starting any process")

    monkeypatch.setattr("evomesh.capabilities.subprocess.Popen", no_process)
    for cwd in ("../", str(root), "outside", "file.txt", "missing", "", "\0", None):
        with pytest.raises(ValueError, match="cwd"):
            tool.handler(**arguments(kind, "print('must not run')", cwd=cwd))
    assert not (root / "missing").exists()


def test_local_javascript_uses_selected_directory(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Local JavaScript requires Node.js")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "note.txt").write_text("synthetic-js")
    result = json.loads(
        execute_code_tool(tmp_path).invoke(
            json.dumps(
                {
                    "language": "javascript",
                    "cwd": "nested",
                    "code": "console.log(require('fs').readFileSync('note.txt', 'utf8'))",
                }
            )
        )
    )
    assert result["exit_code"] == 0 and result["output"] == "synthetic-js\n"


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
def test_cwd_timeout_retains_partial_feedback(tmp_path, kind):
    (tmp_path / "nested").mkdir()
    factory = terminal_tool if kind == "terminal" else execute_code_tool
    tool = factory(tmp_path, timeout=1)
    code = (
        "import time; from pathlib import Path; "
        "Path('receipt.txt').write_text('started'); print('PARTIAL', flush=True); time.sleep(30)"
    )
    with pytest.raises(ExecutionTimeout) as raised:
        tool.invoke(json.dumps(arguments(kind, code, cwd="nested")))
    assert raised.value.output.endswith(b"PARTIAL\n")
    assert (tmp_path / "nested" / "receipt.txt").read_text() == "started"
    assert not (tmp_path / "receipt.txt").exists()


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
def test_app_recovers_from_bad_cwd_with_private_source_linked_evidence(
    store, project, tmp_path, kind
):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "note.txt").write_text("FIXTURE_RESULT")
    code = "from pathlib import Path; print(Path('note.txt').read_text())"

    class Model:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools):
            self.step += 1
            if self.step == 2:
                assert json.loads(messages[-1]["content"])["error"] == "ValueError"
            if self.step == 3:
                assert json.loads(messages[-1]["content"])["output"] == "FIXTURE_RESULT\n"
                return {"role": "assistant", "content": "Recovered"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(self.step),
                        "type": "function",
                        "function": {
                            "name": kind,
                            "arguments": json.dumps(
                                arguments(
                                    kind,
                                    code,
                                    cwd="PRIVATE_MISSING" if self.step == 1 else "nested",
                                )
                            ),
                        },
                    }
                ],
            }

    with AgentApp(
        Config(project=project, workspace=tmp_path, terminal="local"), store=store, model=Model()
    ) as app:
        assert app.run("Synthetic directory task") == "Recovered"
        assert app.last_run_metrics["tool_errors"] == 1
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "tool_error"
    detail = json.loads(rows[0]["detail"])
    assert detail["capability"] == ("terminal" if kind == "terminal" else "execution")
    assert detail["source_paths"] == ["src/evomesh/capabilities.py", "src/evomesh/processes.py"]
    assert "PRIVATE_MISSING" not in json.dumps(rows) and "FIXTURE_RESULT" not in json.dumps(rows)
