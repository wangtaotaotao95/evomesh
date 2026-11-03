"""A small TOML configuration shared by CLI applications and embedded runtimes."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    database_url: str = "sqlite:///.evomesh/ledger.db"
    project: str = "default"
    owner: str = "personal"
    instance: str = field(default_factory=socket.gethostname)
    workspace: Path = Path("workspace")
    repo: Path = field(default_factory=Path.cwd)
    model: str = ""
    base_url: str = ""
    demo: bool = False
    max_steps: int = 12
    history_turns: int = 20
    auto_reflect: bool = False
    terminal: str = "disabled"
    allowed_hosts: list[str] = field(default_factory=list)
    plugin_modules: list[str] = field(default_factory=list)
    mcp_servers: dict[str, list[str]] = field(default_factory=dict)
    channels: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        data = {}
        if path is not None:
            data = tomllib.loads(path.read_text(encoding="utf-8")).get("evomesh", {})
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        env = {
            "database_url": "DATABASE_URL",
            "project": "PROJECT",
            "owner": "OWNER",
            "instance": "INSTANCE_ID",
            "model": "MODEL",
            "base_url": "BASE_URL",
        }
        for key, suffix in env.items():
            if f"EVOMESH_{suffix}" in os.environ:
                data[key] = os.environ[f"EVOMESH_{suffix}"]
        for key in ("workspace", "repo"):
            if key in data:
                value = Path(data[key])
                data[key] = (path.resolve().parent / value).resolve() if path else value.resolve()
        if path and data.get("database_url", "").startswith("sqlite:///"):
            suffix = data["database_url"][10:]
            if suffix != ":memory:" and not Path(suffix).is_absolute():
                data["database_url"] = "sqlite:///" + str(path.resolve().parent / suffix)
        config = cls(**data)
        config.validate()
        return config

    def validate(self):
        from .channels import validate_channels

        validate_channels(self.channels)
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 100:
            raise ValueError("max_steps must be an integer in 1–100")
        if type(self.history_turns) is not int or not 1 <= self.history_turns <= 100:
            raise ValueError("history_turns must be an integer in 1–100")
        if self.terminal not in {"disabled", "local", "docker"}:
            raise ValueError("terminal must be disabled, local or docker")
        if type(self.demo) is not bool or type(self.auto_reflect) is not bool:
            raise ValueError("demo and auto_reflect must be booleans")
        for value in (self.allowed_hosts, self.plugin_modules):
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ValueError("allowed_hosts and plugin_modules must be arrays of strings")
        if not isinstance(self.mcp_servers, dict) or any(
            not isinstance(name, str)
            or not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or not arg for arg in argv)
            for name, argv in self.mcp_servers.items()
        ):
            raise ValueError("mcp_servers maps names to non-empty command arrays")
        for value in (self.project, self.owner, self.instance):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError("project, owner and instance must contain 1–128 characters")
