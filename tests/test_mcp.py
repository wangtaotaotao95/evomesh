import json
import sys

import pytest

pytest.importorskip("mcp")

from evomesh import AgentApp, Config  # noqa: E402
from evomesh.mcp import MCPConnection  # noqa: E402


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
