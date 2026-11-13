"""Capability/source discovery and private execution metrics for whole-stack evolution."""

import json
import time

from sqlalchemy import Column, Index, String, Table, insert, select

from .runtime_store import LONG, OPTIONS, metadata, timestamp

runs = Table(
    "runtime_runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("project", String(128), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("session", String(128), nullable=False),
    Column("instance", String(128), nullable=False),
    Column("status", String(24), nullable=False),
    Column("metrics", LONG, nullable=False),
    timestamp("created_at"),
    **OPTIONS,
)

Index("ix_run_owner", runs.c.project, runs.c.owner, runs.c.created_at)

# Discovery hints, not an allowlist. Any tracked UTF-8 source can still evolve.
CATALOG = {
    "browser": ("Interactive Chromium sessions and page observations", ["browser.py", "app.py"]),
    "terminal": ("Local and restricted Docker commands", ["capabilities.py", "processes.py"]),
    "execution": ("Python/JavaScript code execution", ["capabilities.py", "processes.py"]),
    "loop": ("Reasoning, repeat detection and tool execution", ["agent.py", "tools.py"]),
    "context": ("Context budget and full-turn preservation", ["context.py", "app.py"]),
    "planning": ("Persistent versioned task plans", ["cognition.py", "app.py"]),
    "knowledge": ("Workspace indexing and cited lexical retrieval", ["cognition.py"]),
    "skills": ("Skill discovery and on-demand loading", ["cognition.py", "app.py"]),
    "memory": ("Owner-scoped memory and conversation history", ["runtime_store.py", "app.py"]),
    "runtime": ("SQL tasks, leases and recurring work", ["runtime_store.py", "app.py"]),
    "channels": (
        "Message authentication, normalization and delivery",
        ["channels.py", "channel_store.py"],
    ),
    "models": ("Inference provider adapters", ["models.py"]),
    "mcp": ("MCP tool discovery, calls and model-facing failure feedback", ["mcp.py", "tools.py"]),
    "tools": ("Workspace, terminal, web and MCP tools", ["tools.py", "capabilities.py", "mcp.py"]),
    "console": (
        "Web interface and authenticated APIs",
        ["server.py", "web/app.js", "web/style.css", "web/index.html"],
    ),
    "evaluation": ("Behavioral evaluation and regression gates", ["evaluation.py"]),
    "evolution": (
        "Evidence, reflection and versioned integration",
        ["learning.py", "evolution.py", "proactive.py", "store.py", "cli.py", "processes.py"],
    ),
}


def capability_catalog(repo, tools=()):
    result = []
    for name, (description, files) in CATALOG.items():
        paths = ["src/evomesh/" + path for path in files]
        result.append(
            {
                "name": name,
                "description": description,
                "source_paths": paths,
                "available_in_repo": all((repo / path).is_file() for path in paths),
            }
        )
    result.append(
        {
            "name": "soul",
            "description": "Persona and behavior instructions",
            "source_paths": ["SOUL.md"],
            "available_in_repo": (repo / "SOUL.md").is_file(),
        }
    )
    for item in tools:
        if item.source_paths:
            result.append(
                {
                    "name": f"tool:{item.name}",
                    "description": item.description,
                    "capability": item.capability,
                    "source_paths": list(item.source_paths),
                    "available_in_repo": all((repo / path).is_file() for path in item.source_paths),
                }
            )
    return result


def friction_detail(capability, *, tool=None, error=None, facts=None):
    return json.dumps(
        {
            "capability": capability,
            "source_paths": ["src/evomesh/" + p for p in CATALOG.get(capability, ("", []))[1]],
            **({"tool": tool} if tool else {}),
            **({"error": error} if error else {}),
            **(facts or {}),
        },
        ensure_ascii=False,
    )


class LearningStore:
    def __init__(self, state):
        self.state, self.engine = state, state.engine

    def record(self, identifier, session, instance, metrics):
        with self.engine.begin() as conn:
            conn.execute(
                insert(runs).values(
                    id=identifier,
                    project=self.state.project,
                    owner=self.state.owner,
                    session=session,
                    instance=instance,
                    status=metrics["status"],
                    metrics=json.dumps(metrics),
                    created_at=time.time(),
                )
            )

    def list(self):
        with self.engine.connect() as conn:
            return [
                {**dict(row), "metrics": json.loads(row["metrics"])}
                for row in conn.execute(
                    select(runs)
                    .where(self.state.scope(runs))
                    .order_by(runs.c.created_at.desc())
                    .limit(100)
                ).mappings()
            ]
