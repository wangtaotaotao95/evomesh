import os
import subprocess
import sys
from pathlib import Path

import pytest

from evomesh import AgentApp, Config
from evomesh.scaffold import scaffold


def test_generated_framework_runs_from_another_directory(tmp_path, monkeypatch):
    for key in ("EVOMESH_DATABASE_URL", "EVOMESH_PROJECT", "EVOMESH_OWNER"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "generated"
    scaffold(root, "framework")
    cfg = Config.load(root / "evomesh.toml")
    assert cfg.workspace == root / "workspace"
    assert cfg.repo == root
    assert cfg.database_url == "sqlite:///" + str(root / ".evomesh/ledger.db")
    with AgentApp.from_config(root / "evomesh.toml") as app:
        assert "Workspace files" in app.run("hello", session="main")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.run(
        [sys.executable, "app.py"], cwd=root, env=env, capture_output=True, text=True
    )
    assert process.returncode == 0, process.stderr
    with pytest.raises(ValueError, match="already exists"):
        scaffold(root)


def test_configuration_validation_and_environment(tmp_path, monkeypatch):
    path = tmp_path / "agent.toml"
    path.write_text("[evomesh]\ndemo=true\nmax_steps=0\n")
    with pytest.raises(ValueError):
        Config.load(path)
    path.write_text("[evomesh]\nunknown=true\n")
    with pytest.raises(ValueError, match="Unknown"):
        Config.load(path)
    path.write_text('[evomesh]\nproject="from-file"\n')
    monkeypatch.setenv("EVOMESH_PROJECT", "from-env")
    assert Config.load(path).project == "from-env"
