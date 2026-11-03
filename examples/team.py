"""Explicit parallel delegation with independent application workspaces."""

import tempfile
from pathlib import Path

from evomesh import AgentApp, Team

with tempfile.TemporaryDirectory(prefix="evomesh-team-") as directory:
    root = Path(directory)
    with AgentApp.demo(root / "research") as researcher, AgentApp.demo(root / "writing") as writer:
        team = Team({"researcher": researcher, "writer": writer}, max_workers=2)
        print(team.run({"researcher": "List research files", "writer": "List writing files"}))
        # Attach this tool to a coordinator to enable model-chosen, named delegation:
        # coordinator.register(team.as_tool())
