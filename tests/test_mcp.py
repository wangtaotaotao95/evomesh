import json
import sys

import pytest

pytest.importorskip("mcp")

from evomesh import AgentApp, Config  # noqa: E402
from evomesh.mcp import MCPConnection, MCPToolError  # noqa: E402


@pytest.fixture
def feedback_server(tmp_path):
    server = tmp_path / "feedback_server.py"
    server.write_text("""from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
server = MCPServer("feedback-fixture")
calls = 0
@server.tool()
def lookup(key: str) -> dict:
    global calls
    calls += 1
    if key != "valid":
        raise ToolError("Use key 'valid'. PRIVATE_RECOVERY_FIXTURE")
    return {"found": True, "calls": calls, "is_error": True, "error": "business-data"}
@server.tool(structured_output=False)
def structured_failure() -> CallToolResult:
    return CallToolResult(is_error=True, content=[],
                          structured_content={"hint": "PRIVATE_STRUCTURED_FIXTURE"})
server.run()
""")
    return server


def test_remote_error_allows_model_correction_without_polluting_shared_ledger(
    store, project, tmp_path, feedback_server
):
    class Model:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools):
            self.step += 1
            if self.step == 2:
                result = json.loads(messages[-1]["content"])
                assert result["error"] == "MCPToolError"
                assert "Use key 'valid'" in result["feedback"]
                assert "PRIVATE_RECOVERY_FIXTURE" in result["feedback"]
                assert not result["feedback_truncated"]
            if self.step == 3:
                result = json.loads(messages[-1]["content"])
                assert result == {
                    "found": True,
                    "calls": 2,
                    "is_error": True,
                    "error": "business-data",
                }
                return {"role": "assistant", "content": "Recovered with corrected key"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "lookup",
                        "type": "function",
                        "function": {
                            "name": "catalog__lookup",
                            "arguments": json.dumps(
                                {"key": "invalid" if self.step == 1 else "valid"}
                            ),
                        },
                    }
                ],
            }

    with AgentApp(
        Config(
            project=project,
            workspace=tmp_path / "workspace",
            mcp_servers={"catalog": [sys.executable, str(feedback_server)]},
        ),
        store=store,
        model=Model(),
    ) as app:
        assert app.run("Synthetic lookup", session="recovery") == "Recovered with corrected key"
        assert app.last_run_metrics["tool_calls"] == 2 and app.last_run_metrics["tool_errors"] == 1
        assert "PRIVATE_RECOVERY_FIXTURE" in json.dumps(app.state.session("recovery"))
        assert "PRIVATE_RECOVERY_FIXTURE" not in json.dumps(app.learning.list())
        capability = next(c for c in app.capabilities() if c["name"] == "tool:catalog__lookup")
        assert capability["source_paths"] == ["src/evomesh/mcp.py"]
        assert capability["capability"] == "mcp"
    rows = store.observations(project)
    assert len(rows) == 1 and rows[0]["kind"] == "tool_error"
    detail = json.loads(rows[0]["detail"])
    assert detail["error"] == "MCPToolError" and detail["capability"] == "mcp"
    assert detail["source_paths"] == ["src/evomesh/mcp.py"]
    assert "PRIVATE_RECOVERY_FIXTURE" not in rows[0]["detail"]


def test_framework_mcp_error_retains_runtime_error_compatibility_and_structured_fallback(
    feedback_server,
):
    with MCPConnection("catalog", [sys.executable, str(feedback_server)]) as connection:
        with pytest.raises(RuntimeError) as raised:
            connection.call("lookup", {"key": "invalid"})
        assert isinstance(raised.value, MCPToolError)
        assert "PRIVATE_RECOVERY_FIXTURE" in raised.value.feedback
        assert "PRIVATE_RECOVERY_FIXTURE" not in str(raised.value)
        success = connection.call("lookup", {"key": "valid"})
        assert (json.loads(success) if isinstance(success, str) else success)["calls"] == 2
        with pytest.raises(MCPToolError) as structured:
            connection.call("structured_failure", {})
        assert json.loads(structured.value.feedback) == {"hint": "PRIVATE_STRUCTURED_FIXTURE"}
    assert not connection.thread.is_alive()


def test_real_stdio_server_discovery_call_state_and_cleanup(tmp_path):
    server = tmp_path / "server.py"
    server.write_text("""from mcp.server import MCPServer
server = MCPServer("fixture")
count = 0
@server.tool()
def add(a: int, b: int) -> dict:
    global count
    count += 1
    return {"sum": a + b, "calls": count}
server.run()
""")
    with MCPConnection("math", [sys.executable, str(server)]) as connection:
        tool = connection.tools[0]
        assert tool.name == "math__add"
        assert json.loads(tool.invoke('{"a":2,"b":3}')) == {"sum": 5, "calls": 1}
        assert json.loads(tool.invoke('{"a":1,"b":3}'))["calls"] == 2
    assert not connection.thread.is_alive()
    with AgentApp(
        Config(
            database_url=f"sqlite:///{tmp_path / 'state.db'}",
            demo=True,
            workspace=tmp_path / "workspace",
            mcp_servers={"math": [sys.executable, str(server)]},
        )
    ) as app:
        assert "math__add" in app.tools
        assert json.loads(app.tools["math__add"].invoke('{"a":3,"b":4}'))["sum"] == 7


def test_mcp_startup_error():
    with pytest.raises(RuntimeError, match="startup failed"):
        MCPConnection("bad", ["/does-not-exist/evomesh-test"], timeout=1)
