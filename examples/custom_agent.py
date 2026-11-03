"""Embed EvoMesh with a custom tool and any Model-protocol implementation."""

import os
from pathlib import Path

from evomesh import Agent, Store, Tool
from evomesh.models import ChatModel
from evomesh.tools import object_schema, workspace_tools

Path(".evomesh").mkdir(exist_ok=True)
store = Store(os.getenv("EVOMESH_DATABASE_URL", "sqlite:///.evomesh/ledger.db"))
store.initialize()
tool = Tool(
    "word_count",
    "Count whitespace-separated words.",
    object_schema({"text": {"type": "string"}}),
    lambda text: {"words": len(text.split())},
)
agent = Agent(
    ChatModel(
        os.environ["EVOMESH_MODEL"],
        os.environ["EVOMESH_BASE_URL"],
        os.getenv("EVOMESH_API_KEY", ""),
    ),
    store,
    [*workspace_tools(Path("workspace")), tool],
    project="my-agent",
    instance="laptop",
    repo=Path.cwd(),
)
try:
    print(agent.run("How many words are in 'agents improve from experience'?"))
finally:
    store.close()
