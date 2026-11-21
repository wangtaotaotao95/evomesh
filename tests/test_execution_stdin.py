import hashlib
import json
import shutil
import subprocess
import sys

import pytest

from evomesh import AgentApp, Config
from evomesh.capabilities import execute_code_tool, terminal_tool
from evomesh.processes import CaptureTimeout, capture_output


def invocation(kind, code, **options):
    args = {"command": [sys.executable, "-c", code]} if kind == "terminal" else {"code": code}
    return json.dumps({**args, **options})


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
@pytest.mark.parametrize(
    "payload",
    ["🙂" * 25000, "x" * 50000, '你好\r\n"quote"\0tail'],
    ids=["utf8-byte-limit", "character-limit", "newlines-and-nul"],
)
def test_stdin_and_stdout_progress_concurrently_with_exact_input(tmp_path, kind, payload):
    digest = hashlib.sha256(payload.encode()).hexdigest()
    code = (
        "import sys, hashlib; "
        "sys.stdout.buffer.write(b'x' * 1000000); sys.stdout.flush(); "
        "data = sys.stdin.buffer.read(); print(hashlib.sha256(data).hexdigest())"
    )
    tool = (
        terminal_tool(tmp_path, timeout=5)
        if kind == "terminal"
        else execute_code_tool(tmp_path, timeout=5)
    )
    result = json.loads(tool.invoke(invocation(kind, code, stdin=payload)))
    assert result["exit_code"] == 0 and result["output"].endswith(digest + "\n")
    assert result["output_bytes"] == 1000000 + 65 and result["truncated"]
    assert len(json.dumps(result, ensure_ascii=False)) <= 18000


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
def test_omitted_stdin_is_eof_and_early_reader_close_is_allowed(tmp_path, monkeypatch, kind):
    real_popen = subprocess.Popen
    children = []

    def popen(*args, **kwargs):
        assert kwargs["stdin"] == subprocess.PIPE
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("evomesh.capabilities.subprocess.Popen", popen)
    tool = terminal_tool(tmp_path) if kind == "terminal" else execute_code_tool(tmp_path)
    code = "import sys; assert sys.stdin.buffer.read() == b''; print('EOF')"
    result = json.loads(tool.invoke(invocation(kind, code)))
    assert result["exit_code"] == 0 and result["output"] == "EOF\n"
    code = "import os; os.close(0); print('reader closed')"
    result = json.loads(tool.invoke(invocation(kind, code, stdin="你" * 33333)))
    assert result["exit_code"] == 0 and result["output"] == "reader closed\n"
    assert all(
        child.stdin.closed and child.stdout.closed and child.poll() is not None
        for child in children
    )


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
@pytest.mark.parametrize("backend", ["local", "docker"])
def test_invalid_stdin_is_rejected_before_spawn(tmp_path, monkeypatch, kind, backend):
    factory = terminal_tool if kind == "terminal" else execute_code_tool
    tool = factory(tmp_path, backend=backend)

    def no_spawn(*args, **kwargs):
        pytest.fail("Invalid input must not start a process")

    monkeypatch.setattr("evomesh.capabilities.subprocess.Popen", no_spawn)
    args = {"command": ["unused"]} if kind == "terminal" else {"code": "pass"}
    for value in (None, 1, "x" * 50001, "你" * 33334):
        with pytest.raises(ValueError, match="stdin"):
            tool.handler(**args, stdin=value)


@pytest.mark.parametrize("failure", ["timeout", "guard", "invalid"])
def test_capture_closes_both_pipes_and_reaps_nonreading_child(failure):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,time; print('PARTIAL',flush=True); os.close(1); os.close(2); time.sleep(30)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    checks = []

    def guard():
        checks.append(True)
        if len(checks) > 3:
            raise RuntimeError("Synthetic lease loss")

    try:
        expected = {"timeout": CaptureTimeout, "guard": RuntimeError, "invalid": ValueError}[
            failure
        ]
        with pytest.raises(expected) as raised:
            capture_output(
                process,
                1,
                input_data="invalid" if failure == "invalid" else b"x" * 100000,
                guard=guard if failure == "guard" else None,
            )
        if failure == "timeout":
            assert raised.value.output == b"PARTIAL\n"
        assert process.poll() is not None and process.stdin.closed and process.stdout.closed
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()


def test_local_javascript_can_consume_utf8_stdin(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js is needed for JavaScript execution")
    payload = '{"text":"你好\\nworld"}'
    result = json.loads(
        execute_code_tool(tmp_path).invoke(
            json.dumps(
                {
                    "language": "javascript",
                    "code": "console.log(JSON.parse(require('fs').readFileSync(0,'utf8')).text)",
                    "stdin": payload,
                }
            )
        )
    )
    assert result["exit_code"] == 0 and result["output"] == "你好\nworld\n"


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
def test_stdin_failure_remains_private_in_evolution_evidence(store, project, tmp_path, kind):
    payload = "PRIVATE_INPUT_FIXTURE"
    code = "import sys; print(sys.stdin.read()); raise SystemExit(3)"

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                result = json.loads(messages[-1]["content"])
                assert result["exit_code"] == 3 and result["output"] == payload + "\n"
                return {"role": "assistant", "content": "Observed failure"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "stdin",
                        "type": "function",
                        "function": {
                            "name": kind,
                            "arguments": invocation(kind, code, stdin=payload),
                        },
                    }
                ],
            }

    with AgentApp(
        Config(project=project, workspace=tmp_path, terminal="local"), store=store, model=Model()
    ) as app:
        assert app.run("Synthetic stdin task") == "Observed failure"
        assert app.last_run_metrics["tool_errors"] == 1
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "tool_result_error"
    detail = json.loads(rows[0]["detail"])
    assert detail["capability"] == ("terminal" if kind == "terminal" else "execution")
    assert detail["source_paths"] == ["src/evomesh/capabilities.py", "src/evomesh/processes.py"]
    assert payload not in json.dumps(rows)
