"""A complete framework app: custom tool, memory, conversation, durable task."""

import tempfile

from evomesh import AgentApp, tool


@tool
def word_count(text: str) -> int:
    """Count whitespace-separated words."""
    return len(text.split())


with tempfile.TemporaryDirectory(prefix="evomesh-framework-") as root:
    with AgentApp.demo(root, tools=[word_count]) as app:
        app.state.remember("tone", "Prefer concise answers")
        print(app.run("List my workspace", session="main"))
        identifier = app.submit("List my workspace", key="example")
        app.work_once()
        assert app.state.task(identifier)["status"] == "succeeded"
        print("Task receipts:", app.state.task_events(identifier))
        assert app.state.session("main")
