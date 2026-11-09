import json
import socket
import sys

import httpx
import pytest

from evomesh.capabilities import execute_code_tool, terminal_tool, web_tool
from evomesh.tools import workspace_tools


def test_execute_code_output_errors_timeout_and_secret_hygiene(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOMESH_API_KEY", "private-fixture")
    execute = execute_code_tool(tmp_path)
    result = json.loads(
        execute.invoke(
            json.dumps({"code": "import os; print(6 * 7); print('EVOMESH_API_KEY' in os.environ)"})
        )
    )
    assert result["exit_code"] == 0 and result["output"] == "42\nFalse\n"
    assert json.loads(execute.invoke('{"code":"raise ValueError()"}'))["exit_code"] != 0
    with pytest.raises(TimeoutError):
        execute_code_tool(tmp_path, timeout=0.05).invoke('{"code":"import time; time.sleep(10)"}')


def test_file_search_edit_and_boundaries(tmp_path):
    tools = {t.name: t for t in workspace_tools(tmp_path)}
    (tmp_path / "note.txt").write_text("one\nneedle\n")
    (tmp_path / ".env").write_text("needle secret")
    (tmp_path / "outside").symlink_to(tmp_path.parent)
    result = json.loads(tools["search_files"].invoke('{"query":"needle"}'))
    assert result["matches"] == [{"path": "note.txt", "line": 2, "text": "needle"}]
    tools["edit_file"].invoke('{"path":"note.txt","old":"needle","new":"updated"}')
    assert (tmp_path / "note.txt").read_text() == "one\nupdated\n"
    with pytest.raises(ValueError):
        tools["edit_file"].invoke('{"path":"note.txt","old":"missing","new":"bad"}')
    with pytest.raises(ValueError):
        tools["search_files"].invoke('{"query":"secret","path":"../"}')


def test_nonzero_execution_is_evolution_evidence_without_raw_output(store, project, tmp_path):
    from evomesh import Agent

    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                assert "PRIVATE_FIXTURE" in messages[-1]["content"]
                return {"role": "assistant", "content": "Execution failed"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "code",
                        "type": "function",
                        "function": {
                            "name": "execute_code",
                            "arguments": json.dumps({"code": "print('PRIVATE_FIXTURE'); exit(1)"}),
                        },
                    }
                ],
            }

    agent = Agent(Model(), store, [execute_code_tool(tmp_path)], project=project)
    agent.run("test")
    assert agent.metrics["tool_errors"] == 1
    row = store.observations(project)[0]
    assert row["kind"] == "tool_result_error"
    assert "PRIVATE_FIXTURE" not in row["detail"]
    assert json.loads(row["detail"])["capability"] == "execution"


def test_local_terminal_command_results_environment_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOMESH_API_KEY", "private-value")
    terminal = terminal_tool(tmp_path)
    result = json.loads(
        terminal.invoke(
            json.dumps(
                {
                    "command": [
                        sys.executable,
                        "-c",
                        "import os; print(os.getcwd()); print('EVOMESH_API_KEY' in os.environ)",
                    ]
                }
            )
        )
    )
    assert result["exit_code"] == 0
    assert str(tmp_path) in result["output"]
    assert "False" in result["output"]
    result = json.loads(terminal.invoke(json.dumps({"command": [sys.executable, "-c", "exit(3)"]})))
    assert result["exit_code"] == 3
    with pytest.raises(TimeoutError):
        terminal_tool(tmp_path, timeout=0.05).invoke(
            json.dumps({"command": [sys.executable, "-c", "import time; time.sleep(10)"]})
        )


def test_web_domain_scheme_and_private_address_restrictions(monkeypatch):
    reader = web_tool(["example.com"])
    for url in ("http://example.com", "https://other.example", "https://user:pass@example.com"):
        with pytest.raises(ValueError):
            reader.invoke(json.dumps({"url": url}))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="Private"):
        reader.invoke('{"url":"https://example.com"}')


def test_web_text_extraction_and_redirect_rejection(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))]
    )

    def handler(request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<h1>Hello</h1><script>secret</script><p>World</p>",
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    reader = web_tool(["example.com"])
    result = json.loads(reader.invoke('{"url":"https://example.com"}'))
    assert result["text"] == "Hello\nWorld"
    with pytest.raises(httpx.HTTPStatusError):
        reader.invoke('{"url":"https://example.com/redirect"}')


def test_docker_terminal_has_readonly_workspace_and_nonroot_user(tmp_path):
    import os

    if os.getenv("EVOMESH_TEST_DOCKER") != "1":
        pytest.skip("Enable EVOMESH_TEST_DOCKER with a running Docker engine")
    tmp_path.chmod(0o755)
    (tmp_path / "note.txt").write_text("fixture")
    result = json.loads(
        terminal_tool(tmp_path, backend="docker").invoke(
            json.dumps(
                {
                    "command": [
                        "python",
                        "-c",
                        "import os; from pathlib import Path; "
                        "assert os.getuid() == 65534; print(Path('note.txt').read_text())",
                    ],
                }
            )
        )
    )
    assert result["exit_code"] == 0
    assert "fixture" in result["output"]
    result = json.loads(
        terminal_tool(tmp_path, backend="docker").invoke(
            json.dumps(
                {
                    "command": [
                        "python",
                        "-c",
                        "from pathlib import Path; Path('new.txt').write_text('bad')",
                    ],
                }
            )
        )
    )
    assert result["exit_code"] != 0
    assert not (tmp_path / "new.txt").exists()
