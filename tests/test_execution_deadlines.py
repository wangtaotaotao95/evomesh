import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evomesh.capabilities import execute_code_tool, terminal_tool
from evomesh.evolution import Integrator
from evomesh.proactive import ProactiveEvolution
from evomesh.processes import capture_output

INVALID_SECONDS = [0, -1, float("nan"), float("inf"), float("-inf"), True, "30", None, 10**1000]
INVALID_IDS = [
    "zero",
    "negative",
    "nan",
    "inf",
    "negative-inf",
    "bool",
    "string",
    "none",
    "overflow",
]


@pytest.mark.parametrize("seconds", INVALID_SECONDS, ids=INVALID_IDS)
def test_tool_factories_reject_invalid_timeout_before_workspace_or_process(
    tmp_path, monkeypatch, seconds
):
    def must_not_spawn(*args, **kwargs):
        pytest.fail("Invalid execution settings must not launch a command")

    monkeypatch.setattr("evomesh.capabilities.subprocess.Popen", must_not_spawn)
    workspace = tmp_path / "not-created"
    for factory in (terminal_tool, execute_code_tool):
        for backend in ("local", "docker"):
            with pytest.raises(ValueError, match="timeout must be a finite positive"):
                factory(workspace, backend=backend, timeout=seconds)
    assert not workspace.exists()


@pytest.mark.parametrize("seconds", INVALID_SECONDS, ids=INVALID_IDS)
def test_evolution_rejects_invalid_deadlines_before_git_or_sql(tmp_path, monkeypatch, seconds):
    def must_not_git(*args, **kwargs):
        pytest.fail("Invalid evolution settings must not access Git")

    monkeypatch.setattr("evomesh.evolution.git", must_not_git)
    for field in ("timeout", "lease_seconds"):
        with pytest.raises(ValueError, match=field):
            Integrator(None, tmp_path, "synthetic", [["unused"]], **{field: seconds})
    with pytest.raises(ValueError, match="lease_seconds"):
        ProactiveEvolution(None, lease_seconds=seconds)


@pytest.mark.parametrize("seconds", [0.5, 2.999])
def test_evolution_requires_three_second_minimum_lease(tmp_path, seconds):
    with pytest.raises(ValueError, match="at least 3 seconds"):
        Integrator(None, tmp_path, "synthetic", [["unused"]], lease_seconds=seconds)
    with pytest.raises(ValueError, match="at least 3 seconds"):
        ProactiveEvolution(None, lease_seconds=seconds)


def test_fractional_timeout_and_minimum_lease_are_supported(tmp_path, monkeypatch):
    tool = execute_code_tool(tmp_path, timeout=2.5)
    assert json.loads(tool.invoke('{"code":"print(42)"}'))["output"] == "42\n"
    monkeypatch.setattr("evomesh.evolution.git", lambda *args: "")
    integrator = Integrator(
        None, tmp_path, "synthetic", [["unused"]], timeout=0.25, lease_seconds=3
    )
    assert integrator.timeout == 0.25 and integrator.lease_seconds == 3
    app = SimpleNamespace(
        store=SimpleNamespace(engine=None), config=SimpleNamespace(project="synthetic")
    )
    assert ProactiveEvolution(app, lease_seconds=3).lease_seconds == 3


@pytest.mark.parametrize(
    "arguments",
    [{"timeout": None}, {"timeout": float("nan")}, {"timeout": 0}, {"timeout": 1, "tail_bytes": 0}],
)
def test_low_level_capture_reaps_process_and_closes_pipe_on_invalid_arguments(tmp_path, arguments):
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        with pytest.raises(ValueError):
            capture_output(process, **arguments)
        assert process.poll() is not None
        assert process.stdout.closed
    finally:
        # Keep the regression test safe even if cleanup regresses.
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
